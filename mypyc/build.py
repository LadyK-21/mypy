"""Support for building extensions using mypyc with distutils or setuptools

The main entry point is mypycify, which produces a list of extension
modules to be passed to setup. A trivial setup.py for a mypyc built
project, then, looks like:

    from setuptools import setup
    from mypyc.build import mypycify

    setup(name='test_module',
          ext_modules=mypycify(['foo.py']),
    )

See the mypycify docs for additional arguments.

mypycify can integrate with either distutils or setuptools, but needs
to know at import-time whether it is using distutils or setuputils. We
hackily decide based on whether setuptools has been imported already.
"""

from __future__ import annotations

import hashlib
import os.path
import re
import sys
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NoReturn, Union, cast

from mypy.build import BuildSource
from mypy.errors import CompileError
from mypy.fscache import FileSystemCache
from mypy.main import process_options
from mypy.options import Options
from mypy.util import write_junit_xml
from mypyc.annotate import generate_annotated_html
from mypyc.codegen import emitmodule
from mypyc.common import IS_FREE_THREADED, RUNTIME_C_FILES, shared_lib_name
from mypyc.errors import Errors
from mypyc.ir.pprint import format_modules
from mypyc.namegen import exported_name
from mypyc.options import CompilerOptions

try:
    # Import setuptools so that it monkey-patch overrides distutils
    import setuptools
except ImportError:
    pass

if TYPE_CHECKING:
    if sys.version_info >= (3, 12):
        from setuptools import Extension
    else:
        from distutils.core import Extension as _distutils_Extension
        from typing_extensions import TypeAlias

        from setuptools import Extension as _setuptools_Extension

        Extension: TypeAlias = Union[_setuptools_Extension, _distutils_Extension]

if sys.version_info >= (3, 12):
    # From setuptools' monkeypatch
    from distutils import ccompiler, sysconfig  # type: ignore[import-not-found]
else:
    from distutils import ccompiler, sysconfig


def get_extension() -> type[Extension]:
    # We can work with either setuptools or distutils, and pick setuptools
    # if it has been imported.
    use_setuptools = "setuptools" in sys.modules
    extension_class: type[Extension]

    if sys.version_info < (3, 12) and not use_setuptools:
        import distutils.core

        extension_class = distutils.core.Extension
    else:
        if not use_setuptools:
            sys.exit("error: setuptools not installed")
        extension_class = setuptools.Extension

    return extension_class


def setup_mypycify_vars() -> None:
    """Rewrite a bunch of config vars in pretty dubious ways."""
    # There has to be a better approach to this.

    # The vars can contain ints but we only work with str ones
    vars = cast(dict[str, str], sysconfig.get_config_vars())
    if sys.platform == "darwin":
        # Disable building 32-bit binaries, since we generate too much code
        # for a 32-bit Mach-O object. There has to be a better way to do this.
        vars["LDSHARED"] = vars["LDSHARED"].replace("-arch i386", "")
        vars["LDFLAGS"] = vars["LDFLAGS"].replace("-arch i386", "")
        vars["CFLAGS"] = vars["CFLAGS"].replace("-arch i386", "")


def fail(message: str) -> NoReturn:
    # TODO: Is there something else we should do to fail?
    sys.exit(message)


def emit_messages(options: Options, messages: list[str], dt: float, serious: bool = False) -> None:
    # ... you know, just in case.
    if options.junit_xml:
        py_version = f"{options.python_version[0]}_{options.python_version[1]}"
        write_junit_xml(
            dt,
            serious,
            {None: messages} if messages else {},
            options.junit_xml,
            py_version,
            options.platform,
        )
    if messages:
        print("\n".join(messages))


def get_mypy_config(
    mypy_options: list[str],
    only_compile_paths: Iterable[str] | None,
    compiler_options: CompilerOptions,
    fscache: FileSystemCache | None,
) -> tuple[list[BuildSource], list[BuildSource], Options]:
    """Construct mypy BuildSources and Options from file and options lists"""
    all_sources, options = process_options(mypy_options, fscache=fscache)
    if only_compile_paths is not None:
        paths_set = set(only_compile_paths)
        mypyc_sources = [s for s in all_sources if s.path in paths_set]
    else:
        mypyc_sources = all_sources

    if compiler_options.separate:
        mypyc_sources = [
            src for src in mypyc_sources if src.path and not src.path.endswith("__init__.py")
        ]

    if not mypyc_sources:
        return mypyc_sources, all_sources, options

    # Override whatever python_version is inferred from the .ini file,
    # and set the python_version to be the currently used version.
    options.python_version = sys.version_info[:2]

    if options.python_version[0] == 2:
        fail("Python 2 not supported")
    if not options.strict_optional:
        fail("Disabling strict optional checking not supported")
    options.show_traceback = True
    # Needed to get types for all AST nodes
    options.export_types = True
    # We use mypy incremental mode when doing separate/incremental mypyc compilation
    options.incremental = compiler_options.separate
    options.preserve_asts = True

    for source in mypyc_sources:
        options.per_module_options.setdefault(source.module, {})["mypyc"] = True

    return mypyc_sources, all_sources, options


def generate_c_extension_shim(
    full_module_name: str, module_name: str, dir_name: str, group_name: str
) -> str:
    """Create a C extension shim with a passthrough PyInit function.

    Arguments:
        full_module_name: the dotted full module name
        module_name: the final component of the module name
        dir_name: the directory to place source code
        group_name: the name of the group
    """
    cname = "%s.c" % full_module_name.replace(".", os.sep)
    cpath = os.path.join(dir_name, cname)

    if IS_FREE_THREADED:
        # We use multi-phase init in free-threaded builds to enable free threading.
        shim_name = "module_shim_no_gil_multiphase.tmpl"
    else:
        shim_name = "module_shim.tmpl"

    # We load the C extension shim template from a file.
    # (So that the file could be reused as a bazel template also.)
    with open(os.path.join(include_dir(), shim_name)) as f:
        shim_template = f.read()

    write_file(
        cpath,
        shim_template.format(
            modname=module_name,
            libname=shared_lib_name(group_name),
            full_modname=exported_name(full_module_name),
        ),
    )

    return cpath


def group_name(modules: list[str]) -> str:
    """Produce a probably unique name for a group from a list of module names."""
    if len(modules) == 1:
        return modules[0]

    h = hashlib.sha1()
    h.update(",".join(modules).encode())
    return h.hexdigest()[:20]


def include_dir() -> str:
    """Find the path of the lib-rt dir that needs to be included"""
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), "lib-rt")


def generate_c(
    sources: list[BuildSource],
    options: Options,
    groups: emitmodule.Groups,
    fscache: FileSystemCache,
    compiler_options: CompilerOptions,
) -> tuple[list[list[tuple[str, str]]], str]:
    """Drive the actual core compilation step.

    The groups argument describes how modules are assigned to C
    extension modules. See the comments on the Groups type in
    mypyc.emitmodule for details.

    Returns the C source code and (for debugging) the pretty printed IR.
    """
    t0 = time.time()

    try:
        result = emitmodule.parse_and_typecheck(
            sources, options, compiler_options, groups, fscache
        )
    except CompileError as e:
        emit_messages(options, e.messages, time.time() - t0, serious=(not e.use_stdout))
        sys.exit(1)

    t1 = time.time()
    if result.errors:
        emit_messages(options, result.errors, t1 - t0)
        sys.exit(1)

    if compiler_options.verbose:
        print(f"Parsed and typechecked in {t1 - t0:.3f}s")

    errors = Errors(options)
    modules, ctext, mapper = emitmodule.compile_modules_to_c(
        result, compiler_options=compiler_options, errors=errors, groups=groups
    )
    t2 = time.time()
    emit_messages(options, errors.new_messages(), t2 - t1)
    if errors.num_errors:
        # No need to stop the build if only warnings were emitted.
        sys.exit(1)

    if compiler_options.verbose:
        print(f"Compiled to C in {t2 - t1:.3f}s")

    if options.mypyc_annotation_file:
        generate_annotated_html(options.mypyc_annotation_file, result, modules, mapper)

    return ctext, "\n".join(format_modules(modules))


def build_using_shared_lib(
    sources: list[BuildSource],
    group_name: str,
    cfiles: list[str],
    deps: list[str],
    build_dir: str,
    extra_compile_args: list[str],
) -> list[Extension]:
    """Produce the list of extension modules when a shared library is needed.

    This creates one shared library extension module that all the
    others import, and one shim extension module for each
    module in the build. Each shim simply calls an initialization function
    in the shared library.

    The shared library (which lib_name is the name of) is a Python
    extension module that exports the real initialization functions in
    Capsules stored in module attributes.
    """
    extensions = [
        get_extension()(
            shared_lib_name(group_name),
            sources=cfiles,
            include_dirs=[include_dir(), build_dir],
            depends=deps,
            extra_compile_args=extra_compile_args,
        )
    ]

    for source in sources:
        module_name = source.module.split(".")[-1]
        shim_file = generate_c_extension_shim(source.module, module_name, build_dir, group_name)

        # We include the __init__ in the "module name" we stick in the Extension,
        # since this seems to be needed for it to end up in the right place.
        full_module_name = source.module
        assert source.path
        if os.path.split(source.path)[1] == "__init__.py":
            full_module_name += ".__init__"
        extensions.append(
            get_extension()(
                full_module_name, sources=[shim_file], extra_compile_args=extra_compile_args
            )
        )

    return extensions


def build_single_module(
    sources: list[BuildSource], cfiles: list[str], extra_compile_args: list[str]
) -> list[Extension]:
    """Produce the list of extension modules for a standalone extension.

    This contains just one module, since there is no need for a shared module.
    """
    return [
        get_extension()(
            sources[0].module,
            sources=cfiles,
            include_dirs=[include_dir()],
            extra_compile_args=extra_compile_args,
        )
    ]


def write_file(path: str, contents: str) -> None:
    """Write data into a file.

    If the file already exists and has the same contents we
    want to write, skip writing so as to preserve the mtime
    and avoid triggering recompilation.
    """
    # We encode it ourselves and open the files as binary to avoid windows
    # newline translation
    encoded_contents = contents.encode("utf-8")
    try:
        with open(path, "rb") as f:
            old_contents: bytes | None = f.read()
    except OSError:
        old_contents = None
    if old_contents != encoded_contents:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as g:
            g.write(encoded_contents)

        # Fudge the mtime forward because otherwise when two builds happen close
        # together (like in a test) setuptools might not realize the source is newer
        # than the new artifact.
        # XXX: This is bad though.
        new_mtime = os.stat(path).st_mtime + 1
        os.utime(path, times=(new_mtime, new_mtime))


def construct_groups(
    sources: list[BuildSource],
    separate: bool | list[tuple[list[str], str | None]],
    use_shared_lib: bool,
    group_name_override: str | None,
) -> emitmodule.Groups:
    """Compute Groups given the input source list and separate configs.

    separate is the user-specified configuration for how to assign
    modules to compilation groups (see mypycify docstring for details).

    This takes that and expands it into our internal representation of
    group configuration, documented in mypyc.emitmodule's definition
    of Group.
    """

    if separate is True:
        groups: emitmodule.Groups = [([source], None) for source in sources]
    elif isinstance(separate, list):
        groups = []
        used_sources = set()
        for files, name in separate:
            group_sources = [src for src in sources if src.path in files]
            groups.append((group_sources, name))
            used_sources.update(group_sources)
        unused_sources = [src for src in sources if src not in used_sources]
        if unused_sources:
            groups.extend([([source], None) for source in unused_sources])
    else:
        groups = [(sources, None)]

    # Generate missing names
    for i, (group, name) in enumerate(groups):
        if use_shared_lib and not name:
            if group_name_override is not None:
                name = group_name_override
            else:
                name = group_name([source.module for source in group])
        groups[i] = (group, name)

    return groups


def get_header_deps(cfiles: list[tuple[str, str]]) -> list[str]:
    """Find all the headers used by a group of cfiles.

    We do this by just regexping the source, which is a bit simpler than
    properly plumbing the data through.

    Arguments:
        cfiles: A list of (file name, file contents) pairs.
    """
    headers: set[str] = set()
    for _, contents in cfiles:
        headers.update(re.findall(r'#include "(.*)"', contents))

    return sorted(headers)


def mypyc_build(
    paths: list[str],
    compiler_options: CompilerOptions,
    *,
    separate: bool | list[tuple[list[str], str | None]] = False,
    only_compile_paths: Iterable[str] | None = None,
    skip_cgen_input: Any | None = None,
    always_use_shared_lib: bool = False,
) -> tuple[emitmodule.Groups, list[tuple[list[str], list[str]]]]:
    """Do the front and middle end of mypyc building, producing and writing out C source."""
    fscache = FileSystemCache()
    mypyc_sources, all_sources, options = get_mypy_config(
        paths, only_compile_paths, compiler_options, fscache
    )

    # We generate a shared lib if there are multiple modules or if any
    # of the modules are in package. (Because I didn't want to fuss
    # around with making the single module code handle packages.)
    use_shared_lib = (
        len(mypyc_sources) > 1
        or any("." in x.module for x in mypyc_sources)
        or always_use_shared_lib
    )

    groups = construct_groups(mypyc_sources, separate, use_shared_lib, compiler_options.group_name)

    if compiler_options.group_name is not None:
        assert len(groups) == 1, "If using custom group_name, only one group is expected"

    # We let the test harness just pass in the c file contents instead
    # so that it can do a corner-cutting version without full stubs.
    if not skip_cgen_input:
        group_cfiles, ops_text = generate_c(
            all_sources, options, groups, fscache, compiler_options=compiler_options
        )
        # TODO: unique names?
        write_file(os.path.join(compiler_options.target_dir, "ops.txt"), ops_text)
    else:
        group_cfiles = skip_cgen_input

    # Write out the generated C and collect the files for each group
    # Should this be here??
    group_cfilenames: list[tuple[list[str], list[str]]] = []
    for cfiles in group_cfiles:
        cfilenames = []
        for cfile, ctext in cfiles:
            cfile = os.path.join(compiler_options.target_dir, cfile)
            if not options.mypyc_skip_c_generation:
                write_file(cfile, ctext)
            if os.path.splitext(cfile)[1] == ".c":
                cfilenames.append(cfile)

        deps = [os.path.join(compiler_options.target_dir, dep) for dep in get_header_deps(cfiles)]
        group_cfilenames.append((cfilenames, deps))

    return groups, group_cfilenames


def mypycify(
    paths: list[str],
    *,
    only_compile_paths: Iterable[str] | None = None,
    verbose: bool = False,
    opt_level: str = "3",
    debug_level: str = "1",
    strip_asserts: bool = False,
    multi_file: bool = False,
    separate: bool | list[tuple[list[str], str | None]] = False,
    skip_cgen_input: Any | None = None,
    target_dir: str | None = None,
    include_runtime_files: bool | None = None,
    strict_dunder_typing: bool = False,
    group_name: str | None = None,
    log_trace: bool = False,
) -> list[Extension]:
    """Main entry point to building using mypyc.

    This produces a list of Extension objects that should be passed as the
    ext_modules parameter to setup.

    Arguments:
        paths: A list of file paths to build. It may also contain mypy options.
        only_compile_paths: If not None, an iterable of paths that are to be
                            the only modules compiled, even if other modules
                            appear in the mypy command line given to paths.
                            (These modules must still be passed to paths.)

        verbose: Should mypyc be more verbose. Defaults to false.

        opt_level: The optimization level, as a string. Defaults to '3' (meaning '-O3').
        debug_level: The debug level, as a string. Defaults to '1' (meaning '-g1').
        strip_asserts: Should asserts be stripped from the generated code.

        multi_file: Should each Python module be compiled into its own C source file.
                    This can reduce compile time and memory requirements at the likely
                    cost of runtime performance of compiled code. Defaults to false.
        separate: Should compiled modules be placed in separate extension modules.
                  If False, all modules are placed in a single shared library.
                  If True, every module is placed in its own library.
                  Otherwise, separate should be a list of
                  (file name list, optional shared library name) pairs specifying
                  groups of files that should be placed in the same shared library
                  (while all other modules will be placed in its own library).

                  Each group can be compiled independently, which can
                  speed up compilation, but calls between groups can
                  be slower than calls within a group and can't be
                  inlined.
        target_dir: The directory to write C output files. Defaults to 'build'.
        include_runtime_files: If not None, whether the mypyc runtime library
                               should be directly #include'd instead of linked
                               separately in order to reduce compiler invocations.
                               Defaults to False in multi_file mode, True otherwise.
        strict_dunder_typing: If True, force dunder methods to have the return type
                              of the method strictly, which can lead to more
                              optimization opportunities. Defaults to False.
        group_name: If set, override the default group name derived from
                    the hash of module names. This is used for the names of the
                    output C files and the shared library. This is only supported
                    if there is a single group. [Experimental]
        log_trace: If True, compiled code writes a trace log of events in
                   mypyc_trace.txt (derived from executed operations). This is
                   useful for performance analysis, such as analyzing which
                   primitive ops are used the most and on which lines.
    """

    # Figure out our configuration
    compiler_options = CompilerOptions(
        strip_asserts=strip_asserts,
        multi_file=multi_file,
        verbose=verbose,
        separate=separate is not False,
        target_dir=target_dir,
        include_runtime_files=include_runtime_files,
        strict_dunder_typing=strict_dunder_typing,
        group_name=group_name,
        log_trace=log_trace,
    )

    # Generate all the actual important C code
    groups, group_cfilenames = mypyc_build(
        paths,
        only_compile_paths=only_compile_paths,
        compiler_options=compiler_options,
        separate=separate,
        skip_cgen_input=skip_cgen_input,
    )

    # Mess around with setuptools and actually get the thing built
    setup_mypycify_vars()

    # Create a compiler object so we can make decisions based on what
    # compiler is being used. typeshed is missing some attributes on the
    # compiler object so we give it type Any
    compiler: Any = ccompiler.new_compiler()
    sysconfig.customize_compiler(compiler)

    build_dir = compiler_options.target_dir

    cflags: list[str] = []
    if compiler.compiler_type == "unix":
        cflags += [
            f"-O{opt_level}",
            f"-g{debug_level}",
            "-Werror",
            "-Wno-unused-function",
            "-Wno-unused-label",
            "-Wno-unreachable-code",
            "-Wno-unused-variable",
            "-Wno-unused-command-line-argument",
            "-Wno-unknown-warning-option",
            "-Wno-unused-but-set-variable",
            "-Wno-ignored-optimization-argument",
            # Disables C Preprocessor (cpp) warnings
            # See https://github.com/mypyc/mypyc/issues/956
            "-Wno-cpp",
        ]
        if log_trace:
            cflags.append("-DMYPYC_LOG_TRACE")
    elif compiler.compiler_type == "msvc":
        # msvc doesn't have levels, '/O2' is full and '/Od' is disable
        if opt_level == "0":
            opt_level = "d"
        elif opt_level in ("1", "2", "3"):
            opt_level = "2"
        if debug_level == "0":
            debug_level = "NONE"
        elif debug_level == "1":
            debug_level = "FASTLINK"
        elif debug_level in ("2", "3"):
            debug_level = "FULL"
        cflags += [
            f"/O{opt_level}",
            f"/DEBUG:{debug_level}",
            "/wd4102",  # unreferenced label
            "/wd4101",  # unreferenced local variable
            "/wd4146",  # negating unsigned int
        ]
        if multi_file:
            # Disable whole program optimization in multi-file mode so
            # that we actually get the compilation speed and memory
            # use wins that multi-file mode is intended for.
            cflags += ["/GL-", "/wd9025"]  # warning about overriding /GL
        if log_trace:
            cflags.append("/DMYPYC_LOG_TRACE")

    # If configured to (defaults to yes in multi-file mode), copy the
    # runtime library in. Otherwise it just gets #included to save on
    # compiler invocations.
    shared_cfilenames = []
    if not compiler_options.include_runtime_files:
        for name in RUNTIME_C_FILES:
            rt_file = os.path.join(build_dir, name)
            with open(os.path.join(include_dir(), name), encoding="utf-8") as f:
                write_file(rt_file, f.read())
            shared_cfilenames.append(rt_file)

    extensions = []
    for (group_sources, lib_name), (cfilenames, deps) in zip(groups, group_cfilenames):
        if lib_name:
            extensions.extend(
                build_using_shared_lib(
                    group_sources,
                    lib_name,
                    cfilenames + shared_cfilenames,
                    deps,
                    build_dir,
                    cflags,
                )
            )
        else:
            extensions.extend(
                build_single_module(group_sources, cfilenames + shared_cfilenames, cflags)
            )

    return extensions
