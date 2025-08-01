"""Generate C code for a Python C extension module from Python source code."""

# FIXME: Basically nothing in this file operates on the level of a
#        single module and it should be renamed.

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from typing import Optional, TypeVar

from mypy.build import (
    BuildResult,
    BuildSource,
    State,
    build,
    compute_hash,
    create_metastore,
    get_cache_names,
    sorted_components,
)
from mypy.errors import CompileError
from mypy.fscache import FileSystemCache
from mypy.nodes import MypyFile
from mypy.options import Options
from mypy.plugin import Plugin, ReportConfigContext
from mypy.util import hash_digest, json_dumps
from mypyc.codegen.cstring import c_string_initializer
from mypyc.codegen.emit import Emitter, EmitterContext, HeaderDeclaration, c_array_initializer
from mypyc.codegen.emitclass import generate_class, generate_class_reuse, generate_class_type_decl
from mypyc.codegen.emitfunc import (
    generate_native_function,
    native_function_doc_initializer,
    native_function_header,
)
from mypyc.codegen.emitwrapper import (
    generate_legacy_wrapper_function,
    generate_wrapper_function,
    legacy_wrapper_function_header,
    wrapper_function_header,
)
from mypyc.codegen.literals import Literals
from mypyc.common import (
    IS_FREE_THREADED,
    MODULE_PREFIX,
    PREFIX,
    RUNTIME_C_FILES,
    TOP_LEVEL_NAME,
    TYPE_VAR_PREFIX,
    shared_lib_name,
    short_id_from_name,
)
from mypyc.errors import Errors
from mypyc.ir.func_ir import FuncIR
from mypyc.ir.module_ir import ModuleIR, ModuleIRs, deserialize_modules
from mypyc.ir.ops import DeserMaps, LoadLiteral
from mypyc.ir.rtypes import RType
from mypyc.irbuild.main import build_ir
from mypyc.irbuild.mapper import Mapper
from mypyc.irbuild.prepare import load_type_map
from mypyc.namegen import NameGenerator, exported_name
from mypyc.options import CompilerOptions
from mypyc.transform.copy_propagation import do_copy_propagation
from mypyc.transform.exceptions import insert_exception_handling
from mypyc.transform.flag_elimination import do_flag_elimination
from mypyc.transform.log_trace import insert_event_trace_logging
from mypyc.transform.lower import lower_ir
from mypyc.transform.refcount import insert_ref_count_opcodes
from mypyc.transform.spill import insert_spills
from mypyc.transform.uninit import insert_uninit_checks

# All the modules being compiled are divided into "groups". A group
# is a set of modules that are placed into the same shared library.
# Two common configurations are that every module is placed in a group
# by itself (fully separate compilation) and that every module is
# placed in the same group (fully whole-program compilation), but we
# support finer-grained control of the group as well.
#
# In fully whole-program compilation, we will generate N+1 extension
# modules: one shim per module and one shared library containing all
# the actual code.
# In fully separate compilation, we (unfortunately) will generate 2*N
# extension modules: one shim per module and also one library containing
# each module's actual code. (This might be fixable in the future,
# but allows a clean separation between setup of the export tables
# (see generate_export_table) and running module top levels.)
#
# A group is represented as a list of BuildSources containing all of
# its modules along with the name of the group. (Which can be None
# only if we are compiling only a single group with a single file in it
# and not using shared libraries).
Group = tuple[list[BuildSource], Optional[str]]
Groups = list[Group]

# A list of (file name, file contents) pairs.
FileContents = list[tuple[str, str]]


class MarkedDeclaration:
    """Add a mark, useful for topological sort."""

    def __init__(self, declaration: HeaderDeclaration, mark: bool) -> None:
        self.declaration = declaration
        self.mark = False


class MypycPlugin(Plugin):
    """Plugin for making mypyc interoperate properly with mypy incremental mode.

    Basically the point of this plugin is to force mypy to recheck things
    based on the demands of mypyc in a couple situations:
      * Any modules in the same group must be compiled together, so we
        tell mypy that modules depend on all their groupmates.
      * If the IR metadata is missing or stale or any of the generated
        C source files associated missing or stale, then we need to
        recompile the module so we mark it as stale.
    """

    def __init__(
        self, options: Options, compiler_options: CompilerOptions, groups: Groups
    ) -> None:
        super().__init__(options)
        self.group_map: dict[str, tuple[str | None, list[str]]] = {}
        for sources, name in groups:
            modules = sorted(source.module for source in sources)
            for id in modules:
                self.group_map[id] = (name, modules)

        self.compiler_options = compiler_options
        self.metastore = create_metastore(options)

    def report_config_data(self, ctx: ReportConfigContext) -> tuple[str | None, list[str]] | None:
        # The config data we report is the group map entry for the module.
        # If the data is being used to check validity, we do additional checks
        # that the IR cache exists and matches the metadata cache and all
        # output source files exist and are up to date.

        id, path, is_check = ctx.id, ctx.path, ctx.is_check

        if id not in self.group_map:
            return None

        # If we aren't doing validity checks, just return the cache data
        if not is_check:
            return self.group_map[id]

        # Load the metadata and IR cache
        meta_path, _, _ = get_cache_names(id, path, self.options)
        ir_path = get_ir_cache_name(id, path, self.options)
        try:
            meta_json = self.metastore.read(meta_path)
            ir_json = self.metastore.read(ir_path)
        except FileNotFoundError:
            # This could happen if mypyc failed after mypy succeeded
            # in the previous run or if some cache files got
            # deleted. No big deal, just fail to load the cache.
            return None

        ir_data = json.loads(ir_json)

        # Check that the IR cache matches the metadata cache
        if hash_digest(meta_json) != ir_data["meta_hash"]:
            return None

        # Check that all the source files are present and as
        # expected. The main situation where this would come up is the
        # user deleting the build directory without deleting
        # .mypy_cache, which we should handle gracefully.
        for path, hash in ir_data["src_hashes"].items():
            try:
                with open(os.path.join(self.compiler_options.target_dir, path), "rb") as f:
                    contents = f.read()
            except FileNotFoundError:
                return None
            real_hash = hash_digest(contents)
            if hash != real_hash:
                return None

        return self.group_map[id]

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        # Report dependency on modules in the module's group
        return [(10, id, -1) for id in self.group_map.get(file.fullname, (None, []))[1]]


def parse_and_typecheck(
    sources: list[BuildSource],
    options: Options,
    compiler_options: CompilerOptions,
    groups: Groups,
    fscache: FileSystemCache | None = None,
    alt_lib_path: str | None = None,
) -> BuildResult:
    assert options.strict_optional, "strict_optional must be turned on"
    result = build(
        sources=sources,
        options=options,
        alt_lib_path=alt_lib_path,
        fscache=fscache,
        extra_plugins=[MypycPlugin(options, compiler_options, groups)],
    )
    if result.errors:
        raise CompileError(result.errors)
    return result


def compile_scc_to_ir(
    scc: list[MypyFile],
    result: BuildResult,
    mapper: Mapper,
    compiler_options: CompilerOptions,
    errors: Errors,
) -> ModuleIRs:
    """Compile an SCC into ModuleIRs.

    Any modules that this SCC depends on must have either been compiled,
    type checked, or loaded from a cache into mapper.

    Arguments:
        scc: The list of MypyFiles to compile
        result: The BuildResult from the mypy front-end
        mapper: The Mapper object mapping mypy ASTs to class and func IRs
        compiler_options: The compilation options
        errors: Where to report any errors encountered

    Returns the IR of the modules.
    """

    if compiler_options.verbose:
        print("Compiling {}".format(", ".join(x.name for x in scc)))

    # Generate basic IR, with missing exception and refcount handling.
    modules = build_ir(scc, result.graph, result.types, mapper, compiler_options, errors)
    if errors.num_errors > 0:
        return modules

    env_user_functions = {}
    for module in modules.values():
        for cls in module.classes:
            if cls.env_user_function:
                env_user_functions[cls.env_user_function] = cls

    for module in modules.values():
        for fn in module.functions:
            # Insert checks for uninitialized values.
            insert_uninit_checks(fn)
            # Insert exception handling.
            insert_exception_handling(fn)
            # Insert reference count handling.
            insert_ref_count_opcodes(fn)

            if fn in env_user_functions:
                insert_spills(fn, env_user_functions[fn])

            if compiler_options.log_trace:
                insert_event_trace_logging(fn, compiler_options)

            # Switch to lower abstraction level IR.
            lower_ir(fn, compiler_options)
            # Perform optimizations.
            do_copy_propagation(fn, compiler_options)
            do_flag_elimination(fn, compiler_options)

    return modules


def compile_modules_to_ir(
    result: BuildResult, mapper: Mapper, compiler_options: CompilerOptions, errors: Errors
) -> ModuleIRs:
    """Compile a collection of modules into ModuleIRs.

    The modules to compile are specified as part of mapper's group_map.

    Returns the IR of the modules.
    """
    deser_ctx = DeserMaps({}, {})
    modules = {}

    # Process the graph by SCC in topological order, like we do in mypy.build
    for scc in sorted_components(result.graph):
        scc_states = [result.graph[id] for id in scc]
        trees = [st.tree for st in scc_states if st.id in mapper.group_map and st.tree]

        if not trees:
            continue

        fresh = all(id not in result.manager.rechecked_modules for id in scc)
        if fresh:
            load_scc_from_cache(trees, result, mapper, deser_ctx)
        else:
            scc_ir = compile_scc_to_ir(trees, result, mapper, compiler_options, errors)
            modules.update(scc_ir)

    return modules


def compile_ir_to_c(
    groups: Groups,
    modules: ModuleIRs,
    result: BuildResult,
    mapper: Mapper,
    compiler_options: CompilerOptions,
) -> dict[str | None, list[tuple[str, str]]]:
    """Compile a collection of ModuleIRs to C source text.

    Returns a dictionary mapping group names to a list of (file name,
    file text) pairs.
    """
    source_paths = {
        source.module: result.graph[source.module].xpath
        for sources, _ in groups
        for source in sources
    }

    names = NameGenerator(
        [[source.module for source in sources] for sources, _ in groups],
        separate=compiler_options.separate,
    )

    # Generate C code for each compilation group. Each group will be
    # compiled into a separate extension module.
    ctext: dict[str | None, list[tuple[str, str]]] = {}
    for group_sources, group_name in groups:
        group_modules = {
            source.module: modules[source.module]
            for source in group_sources
            if source.module in modules
        }
        if not group_modules:
            ctext[group_name] = []
            continue
        generator = GroupGenerator(
            group_modules, source_paths, group_name, mapper.group_map, names, compiler_options
        )
        ctext[group_name] = generator.generate_c_for_modules()

    return ctext


def get_ir_cache_name(id: str, path: str, options: Options) -> str:
    meta_path, _, _ = get_cache_names(id, path, options)
    return meta_path.replace(".meta.json", ".ir.json")


def get_state_ir_cache_name(state: State) -> str:
    return get_ir_cache_name(state.id, state.xpath, state.options)


def write_cache(
    modules: ModuleIRs,
    result: BuildResult,
    group_map: dict[str, str | None],
    ctext: dict[str | None, list[tuple[str, str]]],
) -> None:
    """Write out the cache information for modules.

    Each module has the following cache information written (which is
    in addition to the cache information written by mypy itself):
      * A serialized version of its mypyc IR, minus the bodies of
        functions. This allows code that depends on it to use
        these serialized data structures when compiling against it
        instead of needing to recompile it. (Compiling against a
        module requires access to both its mypy and mypyc data
        structures.)
      * The hash of the mypy metadata cache file for the module.
        This is used to ensure that the mypyc cache and the mypy
        cache are in sync and refer to the same version of the code.
        This is particularly important if mypyc crashes/errors/is
        stopped after mypy has written its cache but before mypyc has.
      * The hashes of all the source file outputs for the group
        the module is in. This is so that the module will be
        recompiled if the source outputs are missing.
    """

    hashes = {}
    for name, files in ctext.items():
        hashes[name] = {file: compute_hash(data) for file, data in files}

    # Write out cache data
    for id, module in modules.items():
        st = result.graph[id]

        meta_path, _, _ = get_cache_names(id, st.xpath, result.manager.options)
        # If the metadata isn't there, skip writing the cache.
        try:
            meta_data = result.manager.metastore.read(meta_path)
        except OSError:
            continue

        newpath = get_state_ir_cache_name(st)
        ir_data = {
            "ir": module.serialize(),
            "meta_hash": hash_digest(meta_data),
            "src_hashes": hashes[group_map[id]],
        }

        result.manager.metastore.write(newpath, json_dumps(ir_data))

    result.manager.metastore.commit()


def load_scc_from_cache(
    scc: list[MypyFile], result: BuildResult, mapper: Mapper, ctx: DeserMaps
) -> ModuleIRs:
    """Load IR for an SCC of modules from the cache.

    Arguments and return are as compile_scc_to_ir.
    """
    cache_data = {
        k.fullname: json.loads(
            result.manager.metastore.read(get_state_ir_cache_name(result.graph[k.fullname]))
        )["ir"]
        for k in scc
    }
    modules = deserialize_modules(cache_data, ctx)
    load_type_map(mapper, scc, ctx)
    return modules


def compile_modules_to_c(
    result: BuildResult, compiler_options: CompilerOptions, errors: Errors, groups: Groups
) -> tuple[ModuleIRs, list[FileContents], Mapper]:
    """Compile Python module(s) to the source of Python C extension modules.

    This generates the source code for the "shared library" module
    for each group. The shim modules are generated in mypyc.build.
    Each shared library module provides, for each module in its group,
    a PyCapsule containing an initialization function.
    Additionally, it provides a capsule containing an export table of
    pointers to all the group's functions and static variables.

    Arguments:
        result: The BuildResult from the mypy front-end
        compiler_options: The compilation options
        errors: Where to report any errors encountered
        groups: The groups that we are compiling. See documentation of Groups type above.

    Returns the IR of the modules and a list containing the generated files for each group.
    """
    # Construct a map from modules to what group they belong to
    group_map = {source.module: lib_name for group, lib_name in groups for source in group}
    mapper = Mapper(group_map)

    # Sometimes when we call back into mypy, there might be errors.
    # We don't want to crash when that happens.
    result.manager.errors.set_file(
        "<mypyc>", module=None, scope=None, options=result.manager.options
    )

    modules = compile_modules_to_ir(result, mapper, compiler_options, errors)
    if errors.num_errors > 0:
        return {}, [], Mapper({})

    ctext = compile_ir_to_c(groups, modules, result, mapper, compiler_options)
    write_cache(modules, result, group_map, ctext)

    return modules, [ctext[name] for _, name in groups], mapper


def generate_function_declaration(fn: FuncIR, emitter: Emitter) -> None:
    emitter.context.declarations[emitter.native_function_name(fn.decl)] = HeaderDeclaration(
        f"{native_function_header(fn.decl, emitter)};", needs_export=True
    )
    if fn.name != TOP_LEVEL_NAME and not fn.internal:
        if is_fastcall_supported(fn, emitter.capi_version):
            emitter.context.declarations[PREFIX + fn.cname(emitter.names)] = HeaderDeclaration(
                f"{wrapper_function_header(fn, emitter.names)};"
            )
        else:
            emitter.context.declarations[PREFIX + fn.cname(emitter.names)] = HeaderDeclaration(
                f"{legacy_wrapper_function_header(fn, emitter.names)};"
            )


def pointerize(decl: str, name: str) -> str:
    """Given a C decl and its name, modify it to be a declaration to a pointer."""
    # This doesn't work in general but does work for all our types...
    if "(" in decl:
        # Function pointer. Stick an * in front of the name and wrap it in parens.
        return decl.replace(name, f"(*{name})")
    else:
        # Non-function pointer. Just stick an * in front of the name.
        return decl.replace(name, f"*{name}")


def group_dir(group_name: str) -> str:
    """Given a group name, return the relative directory path for it."""
    return os.sep.join(group_name.split(".")[:-1])


class GroupGenerator:
    def __init__(
        self,
        modules: dict[str, ModuleIR],
        source_paths: dict[str, str],
        group_name: str | None,
        group_map: dict[str, str | None],
        names: NameGenerator,
        compiler_options: CompilerOptions,
    ) -> None:
        """Generator for C source for a compilation group.

        The code for a compilation group contains an internal and an
        external .h file, and then one .c if not in multi_file mode or
        one .c file per module if in multi_file mode.

        Arguments:
            modules: (name, ir) pairs for each module in the group
            source_paths: Map from module names to source file paths
            group_name: The name of the group (or None if this is single-module compilation)
            group_map: A map of modules to their group names
            names: The name generator for the compilation
            compiler_options: Mypyc specific options, including multi_file mode
        """
        self.modules = modules
        self.source_paths = source_paths
        self.context = EmitterContext(names, group_name, group_map)
        self.names = names
        # Initializations of globals to simple values that we can't
        # do statically because the windows loader is bad.
        self.simple_inits: list[tuple[str, str]] = []
        self.group_name = group_name
        self.use_shared_lib = group_name is not None
        self.compiler_options = compiler_options
        self.multi_file = compiler_options.multi_file
        # Multi-phase init is needed to enable free-threading. In the future we'll
        # probably want to enable it always, but we'll wait until it's stable.
        self.multi_phase_init = IS_FREE_THREADED

    @property
    def group_suffix(self) -> str:
        return "_" + exported_name(self.group_name) if self.group_name else ""

    @property
    def short_group_suffix(self) -> str:
        return "_" + exported_name(self.group_name.split(".")[-1]) if self.group_name else ""

    def generate_c_for_modules(self) -> list[tuple[str, str]]:
        file_contents = []
        multi_file = self.use_shared_lib and self.multi_file

        # Collect all literal refs in IR.
        for module in self.modules.values():
            for fn in module.functions:
                collect_literals(fn, self.context.literals)

        base_emitter = Emitter(self.context)
        # Optionally just include the runtime library c files to
        # reduce the number of compiler invocations needed
        if self.compiler_options.include_runtime_files:
            for name in RUNTIME_C_FILES:
                base_emitter.emit_line(f'#include "{name}"')
        base_emitter.emit_line(f'#include "__native{self.short_group_suffix}.h"')
        base_emitter.emit_line(f'#include "__native_internal{self.short_group_suffix}.h"')
        emitter = base_emitter

        self.generate_literal_tables()

        for module_name, module in self.modules.items():
            if multi_file:
                emitter = Emitter(self.context)
                emitter.emit_line(f'#include "__native{self.short_group_suffix}.h"')
                emitter.emit_line(f'#include "__native_internal{self.short_group_suffix}.h"')

            self.declare_module(module_name, emitter)
            self.declare_internal_globals(module_name, emitter)
            self.declare_imports(module.imports, emitter)

            for cl in module.classes:
                if cl.is_ext_class:
                    generate_class(cl, module_name, emitter)

            # Generate Python extension module definitions and module initialization functions.
            self.generate_module_def(emitter, module_name, module)

            for fn in module.functions:
                emitter.emit_line()
                generate_native_function(fn, emitter, self.source_paths[module_name], module_name)
                if fn.name != TOP_LEVEL_NAME and not fn.internal:
                    emitter.emit_line()
                    if is_fastcall_supported(fn, emitter.capi_version):
                        generate_wrapper_function(
                            fn, emitter, self.source_paths[module_name], module_name
                        )
                    else:
                        generate_legacy_wrapper_function(
                            fn, emitter, self.source_paths[module_name], module_name
                        )
            if multi_file:
                name = f"__native_{exported_name(module_name)}.c"
                file_contents.append((name, "".join(emitter.fragments)))

        # The external header file contains type declarations while
        # the internal contains declarations of functions and objects
        # (which are shared between shared libraries via dynamic
        # exports tables and not accessed directly.)
        ext_declarations = Emitter(self.context)
        ext_declarations.emit_line(f"#ifndef MYPYC_NATIVE{self.group_suffix}_H")
        ext_declarations.emit_line(f"#define MYPYC_NATIVE{self.group_suffix}_H")
        ext_declarations.emit_line("#include <Python.h>")
        ext_declarations.emit_line("#include <CPy.h>")

        declarations = Emitter(self.context)
        declarations.emit_line(f"#ifndef MYPYC_NATIVE_INTERNAL{self.group_suffix}_H")
        declarations.emit_line(f"#define MYPYC_NATIVE_INTERNAL{self.group_suffix}_H")
        declarations.emit_line("#include <Python.h>")
        declarations.emit_line("#include <CPy.h>")
        declarations.emit_line(f'#include "__native{self.short_group_suffix}.h"')
        declarations.emit_line()
        declarations.emit_line("int CPyGlobalsInit(void);")
        declarations.emit_line()

        for module_name, module in self.modules.items():
            self.declare_finals(module_name, module.final_names, declarations)
            for cl in module.classes:
                generate_class_type_decl(cl, emitter, ext_declarations, declarations)
                if cl.reuse_freed_instance:
                    generate_class_reuse(cl, emitter, ext_declarations, declarations)
            self.declare_type_vars(module_name, module.type_var_names, declarations)
            for fn in module.functions:
                generate_function_declaration(fn, declarations)

        for lib in sorted(self.context.group_deps):
            elib = exported_name(lib)
            short_lib = exported_name(lib.split(".")[-1])
            declarations.emit_lines(
                "#include <{}>".format(os.path.join(group_dir(lib), f"__native_{short_lib}.h")),
                f"struct export_table_{elib} exports_{elib};",
            )

        sorted_decls = self.toposort_declarations()

        emitter = base_emitter
        self.generate_globals_init(emitter)

        emitter.emit_line()

        for declaration in sorted_decls:
            decls = ext_declarations if declaration.is_type else declarations
            if not declaration.is_type:
                decls.emit_lines(f"extern {declaration.decl[0]}", *declaration.decl[1:])
                # If there is a definition, emit it. Otherwise, repeat the declaration
                # (without an extern).
                if declaration.defn:
                    emitter.emit_lines(*declaration.defn)
                else:
                    emitter.emit_lines(*declaration.decl)
            else:
                decls.emit_lines(*declaration.decl)

        if self.group_name:
            if self.compiler_options.separate:
                self.generate_export_table(ext_declarations, emitter)

            self.generate_shared_lib_init(emitter)

        ext_declarations.emit_line("#endif")
        declarations.emit_line("#endif")

        output_dir = group_dir(self.group_name) if self.group_name else ""
        return file_contents + [
            (
                os.path.join(output_dir, f"__native{self.short_group_suffix}.c"),
                "".join(emitter.fragments),
            ),
            (
                os.path.join(output_dir, f"__native_internal{self.short_group_suffix}.h"),
                "".join(declarations.fragments),
            ),
            (
                os.path.join(output_dir, f"__native{self.short_group_suffix}.h"),
                "".join(ext_declarations.fragments),
            ),
        ]

    def generate_literal_tables(self) -> None:
        """Generate tables containing descriptions of Python literals to construct.

        We will store the constructed literals in a single array that contains
        literals of all types. This way we can refer to an arbitrary literal by
        its index.
        """
        literals = self.context.literals
        # During module initialization we store all the constructed objects here
        self.declare_global("PyObject *[%d]" % literals.num_literals(), "CPyStatics")
        # Descriptions of str literals
        init_str = c_string_array_initializer(literals.encoded_str_values())
        self.declare_global("const char * const []", "CPyLit_Str", initializer=init_str)
        # Descriptions of bytes literals
        init_bytes = c_string_array_initializer(literals.encoded_bytes_values())
        self.declare_global("const char * const []", "CPyLit_Bytes", initializer=init_bytes)
        # Descriptions of int literals
        init_int = c_string_array_initializer(literals.encoded_int_values())
        self.declare_global("const char * const []", "CPyLit_Int", initializer=init_int)
        # Descriptions of float literals
        init_floats = c_array_initializer(literals.encoded_float_values())
        self.declare_global("const double []", "CPyLit_Float", initializer=init_floats)
        # Descriptions of complex literals
        init_complex = c_array_initializer(literals.encoded_complex_values())
        self.declare_global("const double []", "CPyLit_Complex", initializer=init_complex)
        # Descriptions of tuple literals
        init_tuple = c_array_initializer(literals.encoded_tuple_values())
        self.declare_global("const int []", "CPyLit_Tuple", initializer=init_tuple)
        # Descriptions of frozenset literals
        init_frozenset = c_array_initializer(literals.encoded_frozenset_values())
        self.declare_global("const int []", "CPyLit_FrozenSet", initializer=init_frozenset)

    def generate_export_table(self, decl_emitter: Emitter, code_emitter: Emitter) -> None:
        """Generate the declaration and definition of the group's export struct.

        To avoid needing to deal with deeply platform specific issues
        involving dynamic library linking (and some possibly
        insurmountable issues involving cyclic dependencies), compiled
        code accesses functions and data in other compilation groups
        via an explicit "export struct".

        Each group declares a struct type that contains a pointer to
        every function and static variable it exports. It then
        populates this struct and stores a pointer to it in a capsule
        stored as an attribute named 'exports' on the group's shared
        library's python module.

        On load, a group's init function will import all of its
        dependencies' exports tables using the capsule mechanism and
        copy the contents into a local copy of the table (to eliminate
        the need for a pointer indirection when accessing it).

        Then, all calls to functions in another group and accesses to statics
        from another group are done indirectly via the export table.

        For example, a group containing a module b, where b contains a class B
        and a function bar, would declare an export table like:
            struct export_table_b {
                PyTypeObject **CPyType_B;
                PyObject *(*CPyDef_B)(CPyTagged cpy_r_x);
                CPyTagged (*CPyDef_B___foo)(PyObject *cpy_r_self, CPyTagged cpy_r_y);
                tuple_T2OI (*CPyDef_bar)(PyObject *cpy_r_x);
                char (*CPyDef___top_level__)(void);
            };
        that would be initialized with:
            static struct export_table_b exports = {
                &CPyType_B,
                &CPyDef_B,
                &CPyDef_B___foo,
                &CPyDef_bar,
                &CPyDef___top_level__,
            };
        To call `b.foo`, then, a function in another group would do
        `exports_b.CPyDef_bar(...)`.
        """

        decls = decl_emitter.context.declarations

        decl_emitter.emit_lines("", f"struct export_table{self.group_suffix} {{")
        for name, decl in decls.items():
            if decl.needs_export:
                decl_emitter.emit_line(pointerize("\n".join(decl.decl), name))

        decl_emitter.emit_line("};")

        code_emitter.emit_lines("", f"static struct export_table{self.group_suffix} exports = {{")
        for name, decl in decls.items():
            if decl.needs_export:
                code_emitter.emit_line(f"&{name},")

        code_emitter.emit_line("};")

    def generate_shared_lib_init(self, emitter: Emitter) -> None:
        """Generate the init function for a shared library.

        A shared library contains all the actual code for a
        compilation group.

        The init function is responsible for creating Capsules that
        wrap pointers to the initialization function of all the real
        init functions for modules in this shared library as well as
        the export table containing all the exported functions and
        values from all the modules.

        These capsules are stored in attributes of the shared library.
        """
        assert self.group_name is not None

        emitter.emit_line()

        short_name = shared_lib_name(self.group_name).split(".")[-1]

        emitter.emit_lines(
            f"static int exec_{short_name}(PyObject *module)",
            "{",
            "int res;",
            "PyObject *capsule;",
            "PyObject *tmp;",
            "",
        )

        if self.compiler_options.separate:
            emitter.emit_lines(
                'capsule = PyCapsule_New(&exports, "{}.exports", NULL);'.format(
                    shared_lib_name(self.group_name)
                ),
                "if (!capsule) {",
                "goto fail;",
                "}",
                'res = PyObject_SetAttrString(module, "exports", capsule);',
                "Py_DECREF(capsule);",
                "if (res < 0) {",
                "goto fail;",
                "}",
                "",
            )

        for mod in self.modules:
            name = exported_name(mod)
            if self.multi_phase_init:
                capsule_func_prefix = "CPyExec_"
                capsule_name_prefix = "exec_"
                emitter.emit_line(f"extern int CPyExec_{name}(PyObject *);")
            else:
                capsule_func_prefix = "CPyInit_"
                capsule_name_prefix = "init_"
                emitter.emit_line(f"extern PyObject *CPyInit_{name}(void);")
            emitter.emit_lines(
                'capsule = PyCapsule_New((void *){}{}, "{}.{}{}", NULL);'.format(
                    capsule_func_prefix,
                    name,
                    shared_lib_name(self.group_name),
                    capsule_name_prefix,
                    name,
                ),
                "if (!capsule) {",
                "goto fail;",
                "}",
                f'res = PyObject_SetAttrString(module, "{capsule_name_prefix}{name}", capsule);',
                "Py_DECREF(capsule);",
                "if (res < 0) {",
                "goto fail;",
                "}",
                "",
            )

        for group in sorted(self.context.group_deps):
            egroup = exported_name(group)
            emitter.emit_lines(
                'tmp = PyImport_ImportModule("{}"); if (!tmp) goto fail; Py_DECREF(tmp);'.format(
                    shared_lib_name(group)
                ),
                'struct export_table_{} *pexports_{} = PyCapsule_Import("{}.exports", 0);'.format(
                    egroup, egroup, shared_lib_name(group)
                ),
                f"if (!pexports_{egroup}) {{",
                "goto fail;",
                "}",
                "memcpy(&exports_{group}, pexports_{group}, sizeof(exports_{group}));".format(
                    group=egroup
                ),
                "",
            )

        emitter.emit_lines("return 0;", "fail:", "return -1;", "}")

        if self.multi_phase_init:
            emitter.emit_lines(
                f"static PyModuleDef_Slot slots_{short_name}[] = {{",
                f"{{Py_mod_exec, exec_{short_name}}},",
                "{Py_mod_multiple_interpreters, Py_MOD_MULTIPLE_INTERPRETERS_NOT_SUPPORTED},",
                "{Py_mod_gil, Py_MOD_GIL_NOT_USED},",
                "{0, NULL},",
                "};",
            )

        size = 0 if self.multi_phase_init else -1
        emitter.emit_lines(
            f"static PyModuleDef module_def_{short_name} = {{",
            "PyModuleDef_HEAD_INIT,",
            f'.m_name = "{shared_lib_name(self.group_name)}",',
            ".m_doc = NULL,",
            f".m_size = {size},",
            ".m_methods = NULL,",
        )
        if self.multi_phase_init:
            emitter.emit_line(f".m_slots = slots_{short_name},")
        emitter.emit_line("};")

        if self.multi_phase_init:
            emitter.emit_lines(
                f"PyMODINIT_FUNC PyInit_{short_name}(void) {{",
                f"return PyModuleDef_Init(&module_def_{short_name});",
                "}",
            )
        else:
            emitter.emit_lines(
                f"PyMODINIT_FUNC PyInit_{short_name}(void) {{",
                "static PyObject *module = NULL;",
                "if (module) {",
                "Py_INCREF(module);",
                "return module;",
                "}",
                f"module = PyModule_Create(&module_def_{short_name});",
                "if (!module) {",
                "return NULL;",
                "}",
                f"if (exec_{short_name}(module) < 0) {{",
                "Py_DECREF(module);",
                "return NULL;",
                "}",
                "return module;",
                "}",
            )

    def generate_globals_init(self, emitter: Emitter) -> None:
        emitter.emit_lines(
            "",
            "int CPyGlobalsInit(void)",
            "{",
            "static int is_initialized = 0;",
            "if (is_initialized) return 0;",
            "",
        )

        emitter.emit_line("CPy_Init();")
        for symbol, fixup in self.simple_inits:
            emitter.emit_line(f"{symbol} = {fixup};")

        values = "CPyLit_Str, CPyLit_Bytes, CPyLit_Int, CPyLit_Float, CPyLit_Complex, CPyLit_Tuple, CPyLit_FrozenSet"
        emitter.emit_lines(
            f"if (CPyStatics_Initialize(CPyStatics, {values}) < 0) {{", "return -1;", "}"
        )

        emitter.emit_lines("is_initialized = 1;", "return 0;", "}")

    def generate_module_def(self, emitter: Emitter, module_name: str, module: ModuleIR) -> None:
        """Emit the PyModuleDef struct for a module and the module init function."""
        module_prefix = emitter.names.private_name(module_name)
        self.emit_module_methods(emitter, module_name, module_prefix, module)
        self.emit_module_exec_func(emitter, module_name, module_prefix, module)

        # If using multi-phase init and a shared lib, parts of module definition
        # will happen in the shim modules, so we skip some steps here.
        if not (self.multi_phase_init and self.use_shared_lib):
            if self.multi_phase_init:
                self.emit_module_def_slots(emitter, module_prefix, module_name)
            self.emit_module_def_struct(emitter, module_name, module_prefix)
            self.emit_module_init_func(emitter, module_name, module_prefix)

    def emit_module_def_slots(
        self, emitter: Emitter, module_prefix: str, module_name: str
    ) -> None:
        name = f"{module_prefix}_slots"
        exec_name = f"CPyExec_{exported_name(module_name)}"

        emitter.emit_line(f"static PyModuleDef_Slot {name}[] = {{")
        emitter.emit_line(f"{{Py_mod_exec, {exec_name}}},")
        if sys.version_info >= (3, 12):
            # Multiple interpreter support requires not using any C global state,
            # which we don't support yet.
            emitter.emit_line(
                "{Py_mod_multiple_interpreters, Py_MOD_MULTIPLE_INTERPRETERS_NOT_SUPPORTED},"
            )
        if sys.version_info >= (3, 13):
            # Declare support for free-threading to enable experimentation,
            # even if we don't properly support it.
            emitter.emit_line("{Py_mod_gil, Py_MOD_GIL_NOT_USED},")
        emitter.emit_line("{0, NULL},")
        emitter.emit_line("};")

    def emit_module_methods(
        self, emitter: Emitter, module_name: str, module_prefix: str, module: ModuleIR
    ) -> None:
        """Emit module methods (the static PyMethodDef table)."""
        emitter.emit_line(f"static PyMethodDef {module_prefix}module_methods[] = {{")
        for fn in module.functions:
            if fn.class_name is not None or fn.name == TOP_LEVEL_NAME:
                continue
            name = short_id_from_name(fn.name, fn.decl.shortname, fn.line)
            if is_fastcall_supported(fn, emitter.capi_version):
                flag = "METH_FASTCALL"
            else:
                flag = "METH_VARARGS"
            doc = native_function_doc_initializer(fn)
            emitter.emit_line(
                (
                    '{{"{name}", (PyCFunction){prefix}{cname}, {flag} | METH_KEYWORDS, '
                    "{doc} /* docstring */}},"
                ).format(
                    name=name, cname=fn.cname(emitter.names), prefix=PREFIX, flag=flag, doc=doc
                )
            )
        emitter.emit_line("{NULL, NULL, 0, NULL}")
        emitter.emit_line("};")
        emitter.emit_line()

    def emit_module_def_struct(
        self, emitter: Emitter, module_name: str, module_prefix: str
    ) -> None:
        """Emit the static module definition struct (PyModuleDef)."""
        emitter.emit_lines(
            f"static struct PyModuleDef {module_prefix}module = {{",
            "PyModuleDef_HEAD_INIT,",
            f'"{module_name}",',
            "NULL, /* docstring */",
            "0,       /* size of per-interpreter state of the module */",
            f"{module_prefix}module_methods,",
        )
        if self.multi_phase_init and not self.use_shared_lib:
            slots_name = f"{module_prefix}_slots"
            emitter.emit_line(f"{slots_name}, /* m_slots */")
        else:
            emitter.emit_line("NULL,")
        emitter.emit_line("};")
        emitter.emit_line()

    def emit_module_exec_func(
        self, emitter: Emitter, module_name: str, module_prefix: str, module: ModuleIR
    ) -> None:
        """Emit the module exec function.

        If we are compiling just one module, this will be the normal C API
        exec function. If we are compiling 2+ modules, we generate a shared
        library for the modules and shims that call into the shared
        library, and in this case the shared module defines an internal
        exec function for each module and these will be called by the shims
        via Capsules.
        """
        declaration = f"int CPyExec_{exported_name(module_name)}(PyObject *module)"
        module_static = self.module_internal_static_name(module_name, emitter)
        emitter.emit_lines(declaration, "{")
        emitter.emit_line("PyObject* modname = NULL;")
        if self.multi_phase_init:
            emitter.emit_line(f"{module_static} = module;")
        emitter.emit_line(
            f'modname = PyObject_GetAttrString((PyObject *){module_static}, "__name__");'
        )

        module_globals = emitter.static_name("globals", module_name)
        emitter.emit_lines(
            f"{module_globals} = PyModule_GetDict({module_static});",
            f"if (unlikely({module_globals} == NULL))",
            "    goto fail;",
        )

        if self.multi_phase_init:
            emitter.emit_lines(
                f"if (PyModule_AddFunctions(module, {module_prefix}module_methods) < 0)",
                "    goto fail;",
            )

        # HACK: Manually instantiate generated classes here
        type_structs: list[str] = []
        for cl in module.classes:
            type_struct = emitter.type_struct_name(cl)
            type_structs.append(type_struct)
            if cl.is_generated:
                emitter.emit_lines(
                    "{t} = (PyTypeObject *)CPyType_FromTemplate("
                    "(PyObject *){t}_template, NULL, modname);".format(t=type_struct)
                )
                emitter.emit_lines(f"if (unlikely(!{type_struct}))", "    goto fail;")

        emitter.emit_lines("if (CPyGlobalsInit() < 0)", "    goto fail;")

        self.generate_top_level_call(module, emitter)

        emitter.emit_lines("Py_DECREF(modname);")

        emitter.emit_line("return 0;")
        emitter.emit_lines("fail:")
        if self.multi_phase_init:
            emitter.emit_lines(f"{module_static} = NULL;", "Py_CLEAR(modname);")
        else:
            emitter.emit_lines(f"Py_CLEAR({module_static});", "Py_CLEAR(modname);")
        for name, typ in module.final_names:
            static_name = emitter.static_name(name, module_name)
            emitter.emit_dec_ref(static_name, typ, is_xdec=True)
            undef = emitter.c_undefined_value(typ)
            emitter.emit_line(f"{static_name} = {undef};")
        # the type objects returned from CPyType_FromTemplate are all new references
        # so we have to decref them
        for t in type_structs:
            emitter.emit_line(f"Py_CLEAR({t});")
        emitter.emit_line("return -1;")
        emitter.emit_line("}")

    def emit_module_init_func(
        self, emitter: Emitter, module_name: str, module_prefix: str
    ) -> None:
        if not self.use_shared_lib:
            declaration = f"PyMODINIT_FUNC PyInit_{module_name}(void)"
        else:
            declaration = f"PyObject *CPyInit_{exported_name(module_name)}(void)"
        emitter.emit_lines(declaration, "{")

        if self.multi_phase_init:
            def_name = f"{module_prefix}module"
            emitter.emit_line(f"return PyModuleDef_Init(&{def_name});")
            emitter.emit_line("}")
            return

        exec_func = f"CPyExec_{exported_name(module_name)}"

        # Store the module reference in a static and return it when necessary.
        # This is separate from the *global* reference to the module that will
        # be populated when it is imported by a compiled module. We want that
        # reference to only be populated when the module has been successfully
        # imported, whereas this we want to have to stop a circular import.
        module_static = self.module_internal_static_name(module_name, emitter)

        emitter.emit_lines(
            f"if ({module_static}) {{",
            f"Py_INCREF({module_static});",
            f"return {module_static};",
            "}",
        )

        emitter.emit_lines(
            f"{module_static} = PyModule_Create(&{module_prefix}module);",
            f"if (unlikely({module_static} == NULL))",
            "    goto fail;",
        )
        emitter.emit_lines(f"if ({exec_func}({module_static}) != 0)", "    goto fail;")
        emitter.emit_line(f"return {module_static};")
        emitter.emit_lines("fail:", "return NULL;")
        emitter.emit_lines("}")

    def generate_top_level_call(self, module: ModuleIR, emitter: Emitter) -> None:
        """Generate call to function representing module top level."""
        # Optimization: we tend to put the top level last, so reverse iterate
        for fn in reversed(module.functions):
            if fn.name == TOP_LEVEL_NAME:
                emitter.emit_lines(
                    f"char result = {emitter.native_function_name(fn.decl)}();",
                    "if (result == 2)",
                    "    goto fail;",
                )
                break

    def toposort_declarations(self) -> list[HeaderDeclaration]:
        """Topologically sort the declaration dict by dependencies.

        Declarations can require other declarations to come prior in C (such as declaring structs).
        In order to guarantee that the C output will compile the declarations will thus need to
        be properly ordered. This simple DFS guarantees that we have a proper ordering.

        This runs in O(V + E).
        """
        result = []
        marked_declarations: dict[str, MarkedDeclaration] = {}
        for k, v in self.context.declarations.items():
            marked_declarations[k] = MarkedDeclaration(v, False)

        def _toposort_visit(name: str) -> None:
            decl = marked_declarations[name]
            if decl.mark:
                return

            for child in decl.declaration.dependencies:
                _toposort_visit(child)

            result.append(decl.declaration)
            decl.mark = True

        for name in marked_declarations:
            _toposort_visit(name)

        return result

    def declare_global(
        self, type_spaced: str, name: str, *, initializer: str | None = None
    ) -> None:
        if "[" not in type_spaced:
            base = f"{type_spaced}{name}"
        else:
            a, b = type_spaced.split("[", 1)
            base = f"{a}{name}[{b}"

        if not initializer:
            defn = None
        else:
            defn = [f"{base} = {initializer};"]
        if name not in self.context.declarations:
            self.context.declarations[name] = HeaderDeclaration(f"{base};", defn=defn)

    def declare_internal_globals(self, module_name: str, emitter: Emitter) -> None:
        static_name = emitter.static_name("globals", module_name)
        self.declare_global("PyObject *", static_name)

    def module_internal_static_name(self, module_name: str, emitter: Emitter) -> str:
        return emitter.static_name(module_name + "_internal", None, prefix=MODULE_PREFIX)

    def declare_module(self, module_name: str, emitter: Emitter) -> None:
        # We declare two globals for each compiled module:
        # one used internally in the implementation of module init to cache results
        # and prevent infinite recursion in import cycles, and one used
        # by other modules to refer to it.
        if module_name in self.modules:
            internal_static_name = self.module_internal_static_name(module_name, emitter)
            self.declare_global("CPyModule *", internal_static_name, initializer="NULL")
        static_name = emitter.static_name(module_name, None, prefix=MODULE_PREFIX)
        self.declare_global("CPyModule *", static_name)
        self.simple_inits.append((static_name, "Py_None"))

    def declare_imports(self, imps: Iterable[str], emitter: Emitter) -> None:
        for imp in imps:
            self.declare_module(imp, emitter)

    def declare_finals(
        self, module: str, final_names: Iterable[tuple[str, RType]], emitter: Emitter
    ) -> None:
        for name, typ in final_names:
            static_name = emitter.static_name(name, module)
            emitter.context.declarations[static_name] = HeaderDeclaration(
                f"{emitter.ctype_spaced(typ)}{static_name};",
                [self.final_definition(module, name, typ, emitter)],
                needs_export=True,
            )

    def final_definition(self, module: str, name: str, typ: RType, emitter: Emitter) -> str:
        static_name = emitter.static_name(name, module)
        # Here we rely on the fact that undefined value and error value are always the same
        undefined = emitter.c_initializer_undefined_value(typ)
        return f"{emitter.ctype_spaced(typ)}{static_name} = {undefined};"

    def declare_static_pyobject(self, identifier: str, emitter: Emitter) -> None:
        symbol = emitter.static_name(identifier, None)
        self.declare_global("PyObject *", symbol)

    def declare_type_vars(self, module: str, type_var_names: list[str], emitter: Emitter) -> None:
        for name in type_var_names:
            static_name = emitter.static_name(name, module, prefix=TYPE_VAR_PREFIX)
            emitter.context.declarations[static_name] = HeaderDeclaration(
                f"PyObject *{static_name};",
                [f"PyObject *{static_name} = NULL;"],
                needs_export=False,
            )


T = TypeVar("T")


def toposort(deps: dict[T, set[T]]) -> list[T]:
    """Topologically sort a dict from item to dependencies.

    This runs in O(V + E).
    """
    result = []
    visited: set[T] = set()

    def visit(item: T) -> None:
        if item in visited:
            return

        for child in deps[item]:
            visit(child)

        result.append(item)
        visited.add(item)

    for item in deps:
        visit(item)

    return result


def is_fastcall_supported(fn: FuncIR, capi_version: tuple[int, int]) -> bool:
    if fn.class_name is not None:
        if fn.name == "__call__":
            # We can use vectorcalls (PEP 590) when supported
            return True
        # TODO: Support fastcall for __init__.
        return fn.name != "__init__"
    return True


def collect_literals(fn: FuncIR, literals: Literals) -> None:
    """Store all Python literal object refs in fn.

    Collecting literals must happen only after we have the final IR.
    This way we won't include literals that have been optimized away.
    """
    for block in fn.blocks:
        for op in block.ops:
            if isinstance(op, LoadLiteral):
                literals.record_literal(op.value)


def c_string_array_initializer(components: list[bytes]) -> str:
    result = []
    result.append("{\n")
    for s in components:
        result.append("    " + c_string_initializer(s) + ",\n")
    result.append("}")
    return "".join(result)
