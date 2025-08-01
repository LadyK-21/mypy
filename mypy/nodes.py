"""Abstract syntax tree node classes (i.e. parse tree)."""

from __future__ import annotations

import os
from abc import abstractmethod
from collections import defaultdict
from collections.abc import Iterator, Sequence
from enum import Enum, unique
from typing import TYPE_CHECKING, Any, Callable, Final, Optional, TypeVar, Union, cast
from typing_extensions import TypeAlias as _TypeAlias, TypeGuard

from mypy_extensions import trait

import mypy.strconv
from mypy.options import Options
from mypy.util import is_sunder, is_typeshed_file, short_type
from mypy.visitor import ExpressionVisitor, NodeVisitor, StatementVisitor

if TYPE_CHECKING:
    from mypy.patterns import Pattern


class Context:
    """Base type for objects that are valid as error message locations."""

    __slots__ = ("line", "column", "end_line", "end_column")

    def __init__(self, line: int = -1, column: int = -1) -> None:
        self.line = line
        self.column = column
        self.end_line: int | None = None
        self.end_column: int | None = None

    def set_line(
        self,
        target: Context | int,
        column: int | None = None,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> None:
        """If target is a node, pull line (and column) information
        into this node. If column is specified, this will override any column
        information coming from a node.
        """
        if isinstance(target, int):
            self.line = target
        else:
            self.line = target.line
            self.column = target.column
            self.end_line = target.end_line
            self.end_column = target.end_column

        if column is not None:
            self.column = column

        if end_line is not None:
            self.end_line = end_line

        if end_column is not None:
            self.end_column = end_column


if TYPE_CHECKING:
    # break import cycle only needed for mypy
    import mypy.types


T = TypeVar("T")

JsonDict: _TypeAlias = dict[str, Any]


# Symbol table node kinds
#
# TODO rename to use more descriptive names

LDEF: Final = 0
GDEF: Final = 1
MDEF: Final = 2

# Placeholder for a name imported via 'from ... import'. Second phase of
# semantic will replace this the actual imported reference. This is
# needed so that we can detect whether a name has been imported during
# XXX what?
UNBOUND_IMPORTED: Final = 3

# RevealExpr node kinds
REVEAL_TYPE: Final = 0
REVEAL_LOCALS: Final = 1

# Kinds of 'literal' expressions.
#
# Use the function mypy.literals.literal to calculate these.
#
# TODO: Can we make these less confusing?
LITERAL_YES: Final = 2  # Value of expression known statically
LITERAL_TYPE: Final = 1  # Type of expression can be narrowed (e.g. variable reference)
LITERAL_NO: Final = 0  # None of the above

node_kinds: Final = {LDEF: "Ldef", GDEF: "Gdef", MDEF: "Mdef", UNBOUND_IMPORTED: "UnboundImported"}
inverse_node_kinds: Final = {_kind: _name for _name, _kind in node_kinds.items()}


implicit_module_attrs: Final = {
    "__name__": "__builtins__.str",
    "__doc__": None,  # depends on Python version, see semanal.py
    "__path__": None,  # depends on if the module is a package
    "__file__": "__builtins__.str",
    "__package__": "__builtins__.str",
    "__annotations__": None,  # dict[str, Any] bounded in add_implicit_module_attrs()
    "__spec__": None,  # importlib.machinery.ModuleSpec bounded in add_implicit_module_attrs()
}


# These aliases exist because built-in class objects are not subscriptable.
# For example `list[int]` fails at runtime. Instead List[int] should be used.
type_aliases: Final = {
    "typing.List": "builtins.list",
    "typing.Dict": "builtins.dict",
    "typing.Set": "builtins.set",
    "typing.FrozenSet": "builtins.frozenset",
    "typing.ChainMap": "collections.ChainMap",
    "typing.Counter": "collections.Counter",
    "typing.DefaultDict": "collections.defaultdict",
    "typing.Deque": "collections.deque",
    "typing.OrderedDict": "collections.OrderedDict",
    # HACK: a lie in lieu of actual support for PEP 675
    "typing.LiteralString": "builtins.str",
}

# This keeps track of the oldest supported Python version where the corresponding
# alias source is available.
type_aliases_source_versions: Final = {"typing.LiteralString": (3, 11)}

# This keeps track of aliases in `typing_extensions`, which we treat specially.
typing_extensions_aliases: Final = {
    # See: https://github.com/python/mypy/issues/11528
    "typing_extensions.OrderedDict": "collections.OrderedDict",
    # HACK: a lie in lieu of actual support for PEP 675
    "typing_extensions.LiteralString": "builtins.str",
}

reverse_builtin_aliases: Final = {
    "builtins.list": "typing.List",
    "builtins.dict": "typing.Dict",
    "builtins.set": "typing.Set",
    "builtins.frozenset": "typing.FrozenSet",
}


RUNTIME_PROTOCOL_DECOS: Final = (
    "typing.runtime_checkable",
    "typing_extensions.runtime",
    "typing_extensions.runtime_checkable",
)

LAMBDA_NAME: Final = "<lambda>"


class Node(Context):
    """Common base class for all non-type parse tree nodes."""

    __slots__ = ()

    def __str__(self) -> str:
        return self.accept(mypy.strconv.StrConv(options=Options()))

    def str_with_options(self, options: Options) -> str:
        a = self.accept(mypy.strconv.StrConv(options=options))
        assert a
        return a

    def accept(self, visitor: NodeVisitor[T]) -> T:
        raise RuntimeError("Not implemented", type(self))


@trait
class Statement(Node):
    """A statement node."""

    __slots__ = ()

    def accept(self, visitor: StatementVisitor[T]) -> T:
        raise RuntimeError("Not implemented", type(self))


@trait
class Expression(Node):
    """An expression node."""

    __slots__ = ()

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        raise RuntimeError("Not implemented", type(self))


class FakeExpression(Expression):
    """A dummy expression.

    We need a dummy expression in one place, and can't instantiate Expression
    because it is a trait and mypyc barfs.
    """

    __slots__ = ()


# TODO:
# Lvalue = Union['NameExpr', 'MemberExpr', 'IndexExpr', 'SuperExpr', 'StarExpr'
#                'TupleExpr']; see #1783.
Lvalue: _TypeAlias = Expression


@trait
class SymbolNode(Node):
    """Nodes that can be stored in a symbol table."""

    __slots__ = ()

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    # Fully qualified name
    @property
    @abstractmethod
    def fullname(self) -> str:
        pass

    @abstractmethod
    def serialize(self) -> JsonDict:
        pass

    @classmethod
    def deserialize(cls, data: JsonDict) -> SymbolNode:
        classname = data[".class"]
        method = deserialize_map.get(classname)
        if method is not None:
            return method(data)
        raise NotImplementedError(f"unexpected .class {classname}")


# Items: fullname, related symbol table node, surrounding type (if any)
Definition: _TypeAlias = tuple[str, "SymbolTableNode", Optional["TypeInfo"]]


class MypyFile(SymbolNode):
    """The abstract syntax tree of a single source file."""

    __slots__ = (
        "_fullname",
        "path",
        "defs",
        "alias_deps",
        "is_bom",
        "names",
        "imports",
        "ignored_lines",
        "skipped_lines",
        "is_stub",
        "is_cache_skeleton",
        "is_partial_stub_package",
        "plugin_deps",
        "future_import_flags",
        "_is_typeshed_file",
    )

    __match_args__ = ("name", "path", "defs")

    # Fully qualified module name
    _fullname: str
    # Path to the file (empty string if not known)
    path: str
    # Top-level definitions and statements
    defs: list[Statement]
    # Type alias dependencies as mapping from target to set of alias full names
    alias_deps: defaultdict[str, set[str]]
    # Is there a UTF-8 BOM at the start?
    is_bom: bool
    names: SymbolTable
    # All import nodes within the file (also ones within functions etc.)
    imports: list[ImportBase]
    # Lines on which to ignore certain errors when checking.
    # If the value is empty, ignore all errors; otherwise, the list contains all
    # error codes to ignore.
    ignored_lines: dict[int, list[str]]
    # Lines that were skipped during semantic analysis e.g. due to ALWAYS_FALSE, MYPY_FALSE,
    # or platform/version checks. Those lines would not be type-checked.
    skipped_lines: set[int]
    # Is this file represented by a stub file (.pyi)?
    is_stub: bool
    # Is this loaded from the cache and thus missing the actual body of the file?
    is_cache_skeleton: bool
    # Does this represent an __init__.pyi stub with a module __getattr__
    # (i.e. a partial stub package), for such packages we suppress any missing
    # module errors in addition to missing attribute errors.
    is_partial_stub_package: bool
    # Plugin-created dependencies
    plugin_deps: dict[str, set[str]]
    # Future imports defined in this file. Populated during semantic analysis.
    future_import_flags: set[str]
    _is_typeshed_file: bool | None

    def __init__(
        self,
        defs: list[Statement],
        imports: list[ImportBase],
        is_bom: bool = False,
        ignored_lines: dict[int, list[str]] | None = None,
    ) -> None:
        super().__init__()
        self.defs = defs
        self.line = 1  # Dummy line number
        self.column = 0  # Dummy column
        self.imports = imports
        self.is_bom = is_bom
        self.alias_deps = defaultdict(set)
        self.plugin_deps = {}
        if ignored_lines:
            self.ignored_lines = ignored_lines
        else:
            self.ignored_lines = {}
        self.skipped_lines = set()

        self.path = ""
        self.is_stub = False
        self.is_cache_skeleton = False
        self.is_partial_stub_package = False
        self.future_import_flags = set()
        self._is_typeshed_file = None

    def local_definitions(self) -> Iterator[Definition]:
        """Return all definitions within the module (including nested).

        This doesn't include imported definitions.
        """
        return local_definitions(self.names, self.fullname)

    @property
    def name(self) -> str:
        return "" if not self._fullname else self._fullname.split(".")[-1]

    @property
    def fullname(self) -> str:
        return self._fullname

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_mypy_file(self)

    def is_package_init_file(self) -> bool:
        return len(self.path) != 0 and os.path.basename(self.path).startswith("__init__.")

    def is_future_flag_set(self, flag: str) -> bool:
        return flag in self.future_import_flags

    def is_typeshed_file(self, options: Options) -> bool:
        # Cache result since this is called a lot
        if self._is_typeshed_file is None:
            self._is_typeshed_file = is_typeshed_file(options.abs_custom_typeshed_dir, self.path)
        return self._is_typeshed_file

    def serialize(self) -> JsonDict:
        return {
            ".class": "MypyFile",
            "_fullname": self._fullname,
            "names": self.names.serialize(self._fullname),
            "is_stub": self.is_stub,
            "path": self.path,
            "is_partial_stub_package": self.is_partial_stub_package,
            "future_import_flags": list(self.future_import_flags),
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> MypyFile:
        assert data[".class"] == "MypyFile", data
        tree = MypyFile([], [])
        tree._fullname = data["_fullname"]
        tree.names = SymbolTable.deserialize(data["names"])
        tree.is_stub = data["is_stub"]
        tree.path = data["path"]
        tree.is_partial_stub_package = data["is_partial_stub_package"]
        tree.is_cache_skeleton = True
        tree.future_import_flags = set(data["future_import_flags"])
        return tree


class ImportBase(Statement):
    """Base class for all import statements."""

    __slots__ = ("is_unreachable", "is_top_level", "is_mypy_only", "assignments")

    is_unreachable: bool  # Set by semanal.SemanticAnalyzerPass1 if inside `if False` etc.
    is_top_level: bool  # Ditto if outside any class or def
    is_mypy_only: bool  # Ditto if inside `if TYPE_CHECKING` or `if MYPY`

    # If an import replaces existing definitions, we construct dummy assignment
    # statements that assign the imported names to the names in the current scope,
    # for type checking purposes. Example:
    #
    #     x = 1
    #     from m import x   <-- add assignment representing "x = m.x"
    assignments: list[AssignmentStmt]

    def __init__(self) -> None:
        super().__init__()
        self.assignments = []
        self.is_unreachable = False
        self.is_top_level = False
        self.is_mypy_only = False


class Import(ImportBase):
    """import m [as n]"""

    __slots__ = ("ids",)

    __match_args__ = ("ids",)

    ids: list[tuple[str, str | None]]  # (module id, as id)

    def __init__(self, ids: list[tuple[str, str | None]]) -> None:
        super().__init__()
        self.ids = ids

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_import(self)


class ImportFrom(ImportBase):
    """from m import x [as y], ..."""

    __slots__ = ("id", "names", "relative")

    __match_args__ = ("id", "names", "relative")

    id: str
    relative: int
    names: list[tuple[str, str | None]]  # Tuples (name, as name)

    def __init__(self, id: str, relative: int, names: list[tuple[str, str | None]]) -> None:
        super().__init__()
        self.id = id
        self.names = names
        self.relative = relative

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_import_from(self)


class ImportAll(ImportBase):
    """from m import *"""

    __slots__ = ("id", "relative")

    __match_args__ = ("id", "relative")

    id: str
    relative: int

    def __init__(self, id: str, relative: int) -> None:
        super().__init__()
        self.id = id
        self.relative = relative

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_import_all(self)


FUNCBASE_FLAGS: Final = ["is_property", "is_class", "is_static", "is_final"]


class FuncBase(Node):
    """Abstract base class for function-like nodes.

    N.B: Although this has SymbolNode subclasses (FuncDef,
    OverloadedFuncDef), avoid calling isinstance(..., FuncBase) on
    something that is typed as SymbolNode.  This is to work around
    mypy bug #3603, in which mypy doesn't understand multiple
    inheritance very well, and will assume that a SymbolNode
    cannot be a FuncBase.

    Instead, test against SYMBOL_FUNCBASE_TYPES, which enumerates
    SymbolNode subclasses that are also FuncBase subclasses.
    """

    __slots__ = (
        "type",
        "unanalyzed_type",
        "info",
        "is_property",
        "is_class",  # Uses "@classmethod" (explicit or implicit)
        "is_static",  # Uses "@staticmethod" (explicit or implicit)
        "is_final",  # Uses "@final"
        "is_explicit_override",  # Uses "@override"
        "is_type_check_only",  # Uses "@type_check_only"
        "_fullname",
    )

    def __init__(self) -> None:
        super().__init__()
        # Type signature. This is usually CallableType or Overloaded, but it can be
        # something else for decorated functions.
        self.type: mypy.types.ProperType | None = None
        # Original, not semantically analyzed type (used for reprocessing)
        self.unanalyzed_type: mypy.types.ProperType | None = None
        # If method, reference to TypeInfo
        self.info = FUNC_NO_INFO
        self.is_property = False
        self.is_class = False
        # Is this a `@staticmethod` (explicit or implicit)?
        # Note: use has_self_or_cls_argument to check if there is `self` or `cls` argument
        self.is_static = False
        self.is_final = False
        self.is_explicit_override = False
        self.is_type_check_only = False
        # Name with module prefix
        self._fullname = ""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    def fullname(self) -> str:
        return self._fullname

    @property
    def has_self_or_cls_argument(self) -> bool:
        """If used as a method, does it have an argument for method binding (`self`, `cls`)?

        This is true for `__new__` even though `__new__` does not undergo method binding,
        because we still usually assume that `cls` corresponds to the enclosing class.
        """
        return not self.is_static or self.name == "__new__"


OverloadPart: _TypeAlias = Union["FuncDef", "Decorator"]


class OverloadedFuncDef(FuncBase, SymbolNode, Statement):
    """A logical node representing all the variants of a multi-declaration function.

    A multi-declaration function is often an @overload, but can also be a
    @property with a setter and a/or a deleter.

    This node has no explicit representation in the source program.
    Overloaded variants must be consecutive in the source file.
    """

    __slots__ = (
        "items",
        "unanalyzed_items",
        "impl",
        "deprecated",
        "setter_index",
        "_is_trivial_self",
    )

    items: list[OverloadPart]
    unanalyzed_items: list[OverloadPart]
    impl: OverloadPart | None
    deprecated: str | None
    setter_index: int | None

    def __init__(self, items: list[OverloadPart]) -> None:
        super().__init__()
        self.items = items
        self.unanalyzed_items = items.copy()
        self.impl = None
        self.deprecated = None
        self.setter_index = None
        self._is_trivial_self: bool | None = None
        if items:
            # TODO: figure out how to reliably set end position (we don't know the impl here).
            self.set_line(items[0].line, items[0].column)

    @property
    def name(self) -> str:
        if self.items:
            return self.items[0].name
        else:
            # This may happen for malformed overload
            assert self.impl is not None
            return self.impl.name

    @property
    def is_trivial_self(self) -> bool:
        """Check we can use bind_self() fast path for this overload.

        This will return False if at least one overload:
          * Has an explicit self annotation, or Self in signature.
          * Has a non-trivial decorator.
        """
        if self._is_trivial_self is not None:
            return self._is_trivial_self
        for item in self.items:
            if isinstance(item, FuncDef):
                if not item.is_trivial_self:
                    self._is_trivial_self = False
                    return False
            elif item.decorators or not item.func.is_trivial_self:
                self._is_trivial_self = False
                return False
        self._is_trivial_self = True
        return True

    @property
    def setter(self) -> Decorator:
        # Do some consistency checks first.
        first_item = self.items[0]
        assert isinstance(first_item, Decorator)
        assert first_item.var.is_settable_property
        assert self.setter_index is not None
        item = self.items[self.setter_index]
        assert isinstance(item, Decorator)
        return item

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_overloaded_func_def(self)

    def serialize(self) -> JsonDict:
        return {
            ".class": "OverloadedFuncDef",
            "items": [i.serialize() for i in self.items],
            "type": None if self.type is None else self.type.serialize(),
            "fullname": self._fullname,
            "impl": None if self.impl is None else self.impl.serialize(),
            "flags": get_flags(self, FUNCBASE_FLAGS),
            "deprecated": self.deprecated,
            "setter_index": self.setter_index,
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> OverloadedFuncDef:
        assert data[".class"] == "OverloadedFuncDef"
        res = OverloadedFuncDef(
            [cast(OverloadPart, SymbolNode.deserialize(d)) for d in data["items"]]
        )
        if data.get("impl") is not None:
            res.impl = cast(OverloadPart, SymbolNode.deserialize(data["impl"]))
            # set line for empty overload items, as not set in __init__
            if len(res.items) > 0:
                res.set_line(res.impl.line)
        if data.get("type") is not None:
            typ = mypy.types.deserialize_type(data["type"])
            assert isinstance(typ, mypy.types.ProperType)
            res.type = typ
        res._fullname = data["fullname"]
        set_flags(res, data["flags"])
        res.deprecated = data["deprecated"]
        res.setter_index = data["setter_index"]
        # NOTE: res.info will be set in the fixup phase.
        return res

    def is_dynamic(self) -> bool:
        return all(item.is_dynamic() for item in self.items)


class Argument(Node):
    """A single argument in a FuncItem."""

    __slots__ = ("variable", "type_annotation", "initializer", "kind", "pos_only")

    __match_args__ = ("variable", "type_annotation", "initializer", "kind", "pos_only")

    def __init__(
        self,
        variable: Var,
        type_annotation: mypy.types.Type | None,
        initializer: Expression | None,
        kind: ArgKind,
        pos_only: bool = False,
    ) -> None:
        super().__init__()
        self.variable = variable
        self.type_annotation = type_annotation
        self.initializer = initializer
        self.kind = kind  # must be an ARG_* constant
        self.pos_only = pos_only

    def set_line(
        self,
        target: Context | int,
        column: int | None = None,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> None:
        super().set_line(target, column, end_line, end_column)

        if self.initializer and self.initializer.line < 0:
            self.initializer.set_line(self.line, self.column, self.end_line, self.end_column)

        self.variable.set_line(self.line, self.column, self.end_line, self.end_column)


# These specify the kind of a TypeParam
TYPE_VAR_KIND: Final = 0
PARAM_SPEC_KIND: Final = 1
TYPE_VAR_TUPLE_KIND: Final = 2


class TypeParam:
    __slots__ = ("name", "kind", "upper_bound", "values", "default")

    def __init__(
        self,
        name: str,
        kind: int,
        upper_bound: mypy.types.Type | None,
        values: list[mypy.types.Type],
        default: mypy.types.Type | None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.upper_bound = upper_bound
        self.values = values
        self.default = default


FUNCITEM_FLAGS: Final = FUNCBASE_FLAGS + [
    "is_overload",
    "is_generator",
    "is_coroutine",
    "is_async_generator",
    "is_awaitable_coroutine",
]


class FuncItem(FuncBase):
    """Base class for nodes usable as overloaded function items."""

    __slots__ = (
        "arguments",  # Note that can be unset if deserialized (type is a lie!)
        "arg_names",  # Names of arguments
        "arg_kinds",  # Kinds of arguments
        "min_args",  # Minimum number of arguments
        "max_pos",  # Maximum number of positional arguments, -1 if no explicit
        # limit (*args not included)
        "type_args",  # New-style type parameters (PEP 695)
        "body",  # Body of the function
        "is_overload",  # Is this an overload variant of function with more than
        # one overload variant?
        "is_generator",  # Contains a yield statement?
        "is_coroutine",  # Defined using 'async def' syntax?
        "is_async_generator",  # Is an async def generator?
        "is_awaitable_coroutine",  # Decorated with '@{typing,asyncio}.coroutine'?
        "expanded",  # Variants of function with type variables with values expanded
    )

    __deletable__ = ("arguments", "max_pos", "min_args")

    def __init__(
        self,
        arguments: list[Argument] | None = None,
        body: Block | None = None,
        typ: mypy.types.FunctionLike | None = None,
        type_args: list[TypeParam] | None = None,
    ) -> None:
        super().__init__()
        self.arguments = arguments or []
        self.arg_names = [None if arg.pos_only else arg.variable.name for arg in self.arguments]
        self.arg_kinds: list[ArgKind] = [arg.kind for arg in self.arguments]
        self.max_pos: int = self.arg_kinds.count(ARG_POS) + self.arg_kinds.count(ARG_OPT)
        self.type_args: list[TypeParam] | None = type_args
        self.body: Block = body or Block([])
        self.type = typ
        self.unanalyzed_type = typ
        self.is_overload: bool = False
        self.is_generator: bool = False
        self.is_coroutine: bool = False
        self.is_async_generator: bool = False
        self.is_awaitable_coroutine: bool = False
        self.expanded: list[FuncItem] = []

        self.min_args = 0
        for i in range(len(self.arguments)):
            if self.arguments[i] is None and i < self.max_fixed_argc():
                self.min_args = i + 1

    def max_fixed_argc(self) -> int:
        return self.max_pos

    def is_dynamic(self) -> bool:
        return self.type is None


FUNCDEF_FLAGS: Final = FUNCITEM_FLAGS + [
    "is_decorated",
    "is_conditional",
    "is_trivial_body",
    "is_trivial_self",
    "is_mypy_only",
]

# Abstract status of a function
NOT_ABSTRACT: Final = 0
# Explicitly abstract (with @abstractmethod or overload without implementation)
IS_ABSTRACT: Final = 1
# Implicitly abstract: used for functions with trivial bodies defined in Protocols
IMPLICITLY_ABSTRACT: Final = 2


class FuncDef(FuncItem, SymbolNode, Statement):
    """Function definition.

    This is a non-lambda function defined using 'def'.
    """

    __slots__ = (
        "_name",
        "is_decorated",
        "is_conditional",
        "abstract_status",
        "original_def",
        "is_trivial_body",
        "is_trivial_self",
        "is_mypy_only",
        # Present only when a function is decorated with @typing.dataclass_transform or similar
        "dataclass_transform_spec",
        "docstring",
        "deprecated",
    )

    __match_args__ = ("name", "arguments", "type", "body")

    # Note that all __init__ args must have default values
    def __init__(
        self,
        name: str = "",  # Function name
        arguments: list[Argument] | None = None,
        body: Block | None = None,
        typ: mypy.types.FunctionLike | None = None,
        type_args: list[TypeParam] | None = None,
    ) -> None:
        super().__init__(arguments, body, typ, type_args)
        self._name = name
        self.is_decorated = False
        self.is_conditional = False  # Defined conditionally (within block)?
        self.abstract_status = NOT_ABSTRACT
        # Is this an abstract method with trivial body?
        # Such methods can't be called via super().
        self.is_trivial_body = False
        # Original conditional definition
        self.original_def: None | FuncDef | Var | Decorator = None
        # Definitions that appear in if TYPE_CHECKING are marked with this flag.
        self.is_mypy_only = False
        self.dataclass_transform_spec: DataclassTransformSpec | None = None
        self.docstring: str | None = None
        self.deprecated: str | None = None
        # This is used to simplify bind_self() logic in trivial cases (which are
        # the majority). In cases where self is not annotated and there are no Self
        # in the signature we can simply drop the first argument.
        self.is_trivial_self = False

    @property
    def name(self) -> str:
        return self._name

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_func_def(self)

    def serialize(self) -> JsonDict:
        # We're deliberating omitting arguments and storing only arg_names and
        # arg_kinds for space-saving reasons (arguments is not used in later
        # stages of mypy).
        # TODO: After a FuncDef is deserialized, the only time we use `arg_names`
        # and `arg_kinds` is when `type` is None and we need to infer a type. Can
        # we store the inferred type ahead of time?
        return {
            ".class": "FuncDef",
            "name": self._name,
            "fullname": self._fullname,
            "arg_names": self.arg_names,
            "arg_kinds": [int(x.value) for x in self.arg_kinds],
            "type": None if self.type is None else self.type.serialize(),
            "flags": get_flags(self, FUNCDEF_FLAGS),
            "abstract_status": self.abstract_status,
            # TODO: Do we need expanded, original_def?
            "dataclass_transform_spec": (
                None
                if self.dataclass_transform_spec is None
                else self.dataclass_transform_spec.serialize()
            ),
            "deprecated": self.deprecated,
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> FuncDef:
        assert data[".class"] == "FuncDef"
        body = Block([])
        ret = FuncDef(
            data["name"],
            [],
            body,
            (
                None
                if data["type"] is None
                else cast(mypy.types.FunctionLike, mypy.types.deserialize_type(data["type"]))
            ),
        )
        ret._fullname = data["fullname"]
        set_flags(ret, data["flags"])
        # NOTE: ret.info is set in the fixup phase.
        ret.arg_names = data["arg_names"]
        ret.arg_kinds = [ArgKind(x) for x in data["arg_kinds"]]
        ret.abstract_status = data["abstract_status"]
        ret.dataclass_transform_spec = (
            DataclassTransformSpec.deserialize(data["dataclass_transform_spec"])
            if data["dataclass_transform_spec"] is not None
            else None
        )
        ret.deprecated = data["deprecated"]
        # Leave these uninitialized so that future uses will trigger an error
        del ret.arguments
        del ret.max_pos
        del ret.min_args
        return ret


# All types that are both SymbolNodes and FuncBases. See the FuncBase
# docstring for the rationale.
# See https://github.com/python/mypy/pull/13607#issuecomment-1236357236
# TODO: we want to remove this at some point and just use `FuncBase` ideally.
SYMBOL_FUNCBASE_TYPES: Final = (OverloadedFuncDef, FuncDef)


class Decorator(SymbolNode, Statement):
    """A decorated function.

    A single Decorator object can include any number of function decorators.
    """

    __slots__ = ("func", "decorators", "original_decorators", "var", "is_overload")

    __match_args__ = ("decorators", "var", "func")

    func: FuncDef  # Decorated function
    decorators: list[Expression]  # Decorators (may be empty)
    # Some decorators are removed by semanal, keep the original here.
    original_decorators: list[Expression]
    # TODO: This is mostly used for the type; consider replacing with a 'type' attribute
    var: Var  # Represents the decorated function obj
    is_overload: bool

    def __init__(self, func: FuncDef, decorators: list[Expression], var: Var) -> None:
        super().__init__()
        self.func = func
        self.decorators = decorators
        self.original_decorators = decorators.copy()
        self.var = var
        self.is_overload = False

    @property
    def name(self) -> str:
        return self.func.name

    @property
    def fullname(self) -> str:
        return self.func.fullname

    @property
    def is_final(self) -> bool:
        return self.func.is_final

    @property
    def info(self) -> TypeInfo:
        return self.func.info

    @property
    def type(self) -> mypy.types.Type | None:
        return self.var.type

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_decorator(self)

    def serialize(self) -> JsonDict:
        return {
            ".class": "Decorator",
            "func": self.func.serialize(),
            "var": self.var.serialize(),
            "is_overload": self.is_overload,
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> Decorator:
        assert data[".class"] == "Decorator"
        dec = Decorator(FuncDef.deserialize(data["func"]), [], Var.deserialize(data["var"]))
        dec.is_overload = data["is_overload"]
        return dec

    def is_dynamic(self) -> bool:
        return self.func.is_dynamic()


VAR_FLAGS: Final = [
    "is_self",
    "is_cls",
    "is_initialized_in_class",
    "is_staticmethod",
    "is_classmethod",
    "is_property",
    "is_settable_property",
    "is_suppressed_import",
    "is_classvar",
    "is_abstract_var",
    "is_final",
    "is_index_var",
    "final_unset_in_class",
    "final_set_in_init",
    "explicit_self_type",
    "is_ready",
    "is_inferred",
    "invalid_partial_type",
    "from_module_getattr",
    "has_explicit_value",
    "allow_incompatible_override",
]


class Var(SymbolNode):
    """A variable.

    It can refer to global/local variable or a data attribute.
    """

    __slots__ = (
        "_name",
        "_fullname",
        "info",
        "type",
        "setter_type",
        "final_value",
        "is_self",
        "is_cls",
        "is_ready",
        "is_inferred",
        "is_initialized_in_class",
        "is_staticmethod",
        "is_classmethod",
        "is_property",
        "is_settable_property",
        "is_classvar",
        "is_abstract_var",
        "is_final",
        "is_index_var",
        "final_unset_in_class",
        "final_set_in_init",
        "is_suppressed_import",
        "explicit_self_type",
        "from_module_getattr",
        "has_explicit_value",
        "allow_incompatible_override",
        "invalid_partial_type",
    )

    __match_args__ = ("name", "type", "final_value")

    def __init__(self, name: str, type: mypy.types.Type | None = None) -> None:
        super().__init__()
        self._name = name  # Name without module prefix
        # TODO: Should be Optional[str]
        self._fullname = ""  # Name with module prefix
        # TODO: Should be Optional[TypeInfo]
        self.info = VAR_NO_INFO
        self.type: mypy.types.Type | None = type  # Declared or inferred type, or None
        # The setter type for settable properties.
        self.setter_type: mypy.types.CallableType | None = None
        # Is this the first argument to an ordinary method (usually "self")?
        self.is_self = False
        # Is this the first argument to a classmethod (typically "cls")?
        self.is_cls = False
        self.is_ready = True  # If inferred, is the inferred type available?
        self.is_inferred = self.type is None
        # Is this initialized explicitly to a non-None value in class body?
        self.is_initialized_in_class = False
        self.is_staticmethod = False
        self.is_classmethod = False
        self.is_property = False
        self.is_settable_property = False
        self.is_classvar = False
        self.is_abstract_var = False
        self.is_index_var = False
        # Set to true when this variable refers to a module we were unable to
        # parse for some reason (eg a silenced module)
        self.is_suppressed_import = False
        # Was this "variable" (rather a constant) defined as Final[...]?
        self.is_final = False
        # If constant value is a simple literal,
        # store the literal value (unboxed) for the benefit of
        # tools like mypyc.
        self.final_value: int | float | complex | bool | str | None = None
        # Where the value was set (only for class attributes)
        self.final_unset_in_class = False
        self.final_set_in_init = False
        # This is True for a variable that was declared on self with an explicit type:
        #     class C:
        #         def __init__(self) -> None:
        #             self.x: int
        # This case is important because this defines a new Var, even if there is one
        # present in a superclass (without explicit type this doesn't create a new Var).
        # See SemanticAnalyzer.analyze_member_lvalue() for details.
        self.explicit_self_type = False
        # If True, this is an implicit Var created due to module-level __getattr__.
        self.from_module_getattr = False
        # Var can be created with an explicit value `a = 1` or without one `a: int`,
        # we need a way to tell which one is which.
        self.has_explicit_value = False
        # If True, subclasses can override this with an incompatible type.
        self.allow_incompatible_override = False
        # If True, this means we didn't manage to infer full type and fall back to
        # something like list[Any]. We may decide to not use such types as context.
        self.invalid_partial_type = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def fullname(self) -> str:
        return self._fullname

    def __repr__(self) -> str:
        name = self.fullname or self.name
        return f"<Var {name!r} at {hex(id(self))}>"

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_var(self)

    def serialize(self) -> JsonDict:
        # TODO: Leave default values out?
        # NOTE: Sometimes self.is_ready is False here, but we don't care.
        data: JsonDict = {
            ".class": "Var",
            "name": self._name,
            "fullname": self._fullname,
            "type": None if self.type is None else self.type.serialize(),
            "setter_type": None if self.setter_type is None else self.setter_type.serialize(),
            "flags": get_flags(self, VAR_FLAGS),
        }
        if self.final_value is not None:
            data["final_value"] = self.final_value
        return data

    @classmethod
    def deserialize(cls, data: JsonDict) -> Var:
        assert data[".class"] == "Var"
        name = data["name"]
        type = None if data["type"] is None else mypy.types.deserialize_type(data["type"])
        setter_type = (
            None
            if data["setter_type"] is None
            else mypy.types.deserialize_type(data["setter_type"])
        )
        v = Var(name, type)
        assert (
            setter_type is None
            or isinstance(setter_type, mypy.types.ProperType)
            and isinstance(setter_type, mypy.types.CallableType)
        )
        v.setter_type = setter_type
        v.is_ready = False  # Override True default set in __init__
        v._fullname = data["fullname"]
        set_flags(v, data["flags"])
        v.final_value = data.get("final_value")
        return v


class ClassDef(Statement):
    """Class definition"""

    __slots__ = (
        "name",
        "_fullname",
        "defs",
        "type_args",
        "type_vars",
        "base_type_exprs",
        "removed_base_type_exprs",
        "info",
        "metaclass",
        "decorators",
        "keywords",
        "analyzed",
        "has_incompatible_baseclass",
        "docstring",
        "removed_statements",
    )

    __match_args__ = ("name", "defs")

    name: str  # Name of the class without module prefix
    _fullname: str  # Fully qualified name of the class
    defs: Block
    # New-style type parameters (PEP 695), unanalyzed
    type_args: list[TypeParam] | None
    # Semantically analyzed type parameters (all syntax variants)
    type_vars: list[mypy.types.TypeVarLikeType]
    # Base class expressions (not semantically analyzed -- can be arbitrary expressions)
    base_type_exprs: list[Expression]
    # Special base classes like Generic[...] get moved here during semantic analysis
    removed_base_type_exprs: list[Expression]
    info: TypeInfo  # Related TypeInfo
    metaclass: Expression | None
    decorators: list[Expression]
    keywords: dict[str, Expression]
    analyzed: Expression | None
    has_incompatible_baseclass: bool
    # Used by special forms like NamedTuple and TypedDict to store invalid statements
    removed_statements: list[Statement]

    def __init__(
        self,
        name: str,
        defs: Block,
        type_vars: list[mypy.types.TypeVarLikeType] | None = None,
        base_type_exprs: list[Expression] | None = None,
        metaclass: Expression | None = None,
        keywords: list[tuple[str, Expression]] | None = None,
        type_args: list[TypeParam] | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self._fullname = ""
        self.defs = defs
        self.type_vars = type_vars or []
        self.type_args = type_args
        self.base_type_exprs = base_type_exprs or []
        self.removed_base_type_exprs = []
        self.info = CLASSDEF_NO_INFO
        self.metaclass = metaclass
        self.decorators = []
        self.keywords = dict(keywords) if keywords else {}
        self.analyzed = None
        self.has_incompatible_baseclass = False
        self.docstring: str | None = None
        self.removed_statements = []

    @property
    def fullname(self) -> str:
        return self._fullname

    @fullname.setter
    def fullname(self, v: str) -> None:
        self._fullname = v

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_class_def(self)

    def is_generic(self) -> bool:
        return self.info.is_generic()

    def serialize(self) -> JsonDict:
        # Not serialized: defs, base_type_exprs, metaclass, decorators,
        # analyzed (for named tuples etc.)
        return {
            ".class": "ClassDef",
            "name": self.name,
            "fullname": self.fullname,
            "type_vars": [v.serialize() for v in self.type_vars],
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> ClassDef:
        assert data[".class"] == "ClassDef"
        res = ClassDef(
            data["name"],
            Block([]),
            # https://github.com/python/mypy/issues/12257
            [
                cast(mypy.types.TypeVarLikeType, mypy.types.deserialize_type(v))
                for v in data["type_vars"]
            ],
        )
        res.fullname = data["fullname"]
        return res


class GlobalDecl(Statement):
    """Declaration global x, y, ..."""

    __slots__ = ("names",)

    __match_args__ = ("names",)

    names: list[str]

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_global_decl(self)


class NonlocalDecl(Statement):
    """Declaration nonlocal x, y, ..."""

    __slots__ = ("names",)

    __match_args__ = ("names",)

    names: list[str]

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_nonlocal_decl(self)


class Block(Statement):
    __slots__ = ("body", "is_unreachable")

    __match_args__ = ("body", "is_unreachable")

    def __init__(self, body: list[Statement], *, is_unreachable: bool = False) -> None:
        super().__init__()
        self.body = body
        # True if we can determine that this block is not executed during semantic
        # analysis. For example, this applies to blocks that are protected by
        # something like "if PY3:" when using Python 2. However, some code is
        # only considered unreachable during type checking and this is not true
        # in those cases.
        self.is_unreachable = is_unreachable

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_block(self)


# Statements


class ExpressionStmt(Statement):
    """An expression as a statement, such as print(s)."""

    __slots__ = ("expr",)

    __match_args__ = ("expr",)

    expr: Expression

    def __init__(self, expr: Expression) -> None:
        super().__init__()
        self.expr = expr

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_expression_stmt(self)


class AssignmentStmt(Statement):
    """Assignment statement.

    The same node class is used for single assignment, multiple assignment
    (e.g. x, y = z) and chained assignment (e.g. x = y = z), assignments
    that define new names, and assignments with explicit types ("# type: t"
    or "x: t [= ...]").

    An lvalue can be NameExpr, TupleExpr, ListExpr, MemberExpr, or IndexExpr.
    """

    __slots__ = (
        "lvalues",
        "rvalue",
        "type",
        "unanalyzed_type",
        "new_syntax",
        "is_alias_def",
        "is_final_def",
        "invalid_recursive_alias",
    )

    __match_args__ = ("lvalues", "rvalues", "type")

    lvalues: list[Lvalue]
    # This is a TempNode if and only if no rvalue (x: t).
    rvalue: Expression
    # Declared type in a comment, may be None.
    type: mypy.types.Type | None
    # Original, not semantically analyzed type in annotation (used for reprocessing)
    unanalyzed_type: mypy.types.Type | None
    # This indicates usage of PEP 526 type annotation syntax in assignment.
    new_syntax: bool
    # Does this assignment define a type alias?
    is_alias_def: bool
    # Is this a final definition?
    # Final attributes can't be re-assigned once set, and can't be overridden
    # in a subclass. This flag is not set if an attempted declaration was found to
    # be invalid during semantic analysis. It is still set to `True` if
    # a final declaration overrides another final declaration (this is checked
    # during type checking when MROs are known).
    is_final_def: bool
    # Stop further processing of this assignment, to prevent flipping back and forth
    # during semantic analysis passes.
    invalid_recursive_alias: bool

    def __init__(
        self,
        lvalues: list[Lvalue],
        rvalue: Expression,
        type: mypy.types.Type | None = None,
        new_syntax: bool = False,
    ) -> None:
        super().__init__()
        self.lvalues = lvalues
        self.rvalue = rvalue
        self.type = type
        self.unanalyzed_type = type
        self.new_syntax = new_syntax
        self.is_alias_def = False
        self.is_final_def = False
        self.invalid_recursive_alias = False

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_assignment_stmt(self)


class OperatorAssignmentStmt(Statement):
    """Operator assignment statement such as x += 1"""

    __slots__ = ("op", "lvalue", "rvalue")

    __match_args__ = ("lvalue", "op", "rvalue")

    op: str  # TODO: Enum?
    lvalue: Lvalue
    rvalue: Expression

    def __init__(self, op: str, lvalue: Lvalue, rvalue: Expression) -> None:
        super().__init__()
        self.op = op
        self.lvalue = lvalue
        self.rvalue = rvalue

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_operator_assignment_stmt(self)


class WhileStmt(Statement):
    __slots__ = ("expr", "body", "else_body")

    __match_args__ = ("expr", "body", "else_body")

    expr: Expression
    body: Block
    else_body: Block | None

    def __init__(self, expr: Expression, body: Block, else_body: Block | None) -> None:
        super().__init__()
        self.expr = expr
        self.body = body
        self.else_body = else_body

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_while_stmt(self)


class ForStmt(Statement):
    __slots__ = (
        "index",
        "index_type",
        "unanalyzed_index_type",
        "inferred_item_type",
        "inferred_iterator_type",
        "expr",
        "body",
        "else_body",
        "is_async",
    )

    __match_args__ = ("index", "index_type", "expr", "body", "else_body")

    # Index variables
    index: Lvalue
    # Type given by type comments for index, can be None
    index_type: mypy.types.Type | None
    # Original, not semantically analyzed type in annotation (used for reprocessing)
    unanalyzed_index_type: mypy.types.Type | None
    # Inferred iterable item type
    inferred_item_type: mypy.types.Type | None
    # Inferred iterator type
    inferred_iterator_type: mypy.types.Type | None
    # Expression to iterate
    expr: Expression
    body: Block
    else_body: Block | None
    is_async: bool  # True if `async for ...` (PEP 492, Python 3.5)

    def __init__(
        self,
        index: Lvalue,
        expr: Expression,
        body: Block,
        else_body: Block | None,
        index_type: mypy.types.Type | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.index_type = index_type
        self.unanalyzed_index_type = index_type
        self.inferred_item_type = None
        self.inferred_iterator_type = None
        self.expr = expr
        self.body = body
        self.else_body = else_body
        self.is_async = False

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_for_stmt(self)


class ReturnStmt(Statement):
    __slots__ = ("expr",)

    __match_args__ = ("expr",)

    expr: Expression | None

    def __init__(self, expr: Expression | None) -> None:
        super().__init__()
        self.expr = expr

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_return_stmt(self)


class AssertStmt(Statement):
    __slots__ = ("expr", "msg")

    __match_args__ = ("expr", "msg")

    expr: Expression
    msg: Expression | None

    def __init__(self, expr: Expression, msg: Expression | None = None) -> None:
        super().__init__()
        self.expr = expr
        self.msg = msg

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_assert_stmt(self)


class DelStmt(Statement):
    __slots__ = ("expr",)

    __match_args__ = ("expr",)

    expr: Lvalue

    def __init__(self, expr: Lvalue) -> None:
        super().__init__()
        self.expr = expr

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_del_stmt(self)


class BreakStmt(Statement):
    __slots__ = ()

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_break_stmt(self)


class ContinueStmt(Statement):
    __slots__ = ()

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_continue_stmt(self)


class PassStmt(Statement):
    __slots__ = ()

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_pass_stmt(self)


class IfStmt(Statement):
    __slots__ = ("expr", "body", "else_body")

    __match_args__ = ("expr", "body", "else_body")

    expr: list[Expression]
    body: list[Block]
    else_body: Block | None

    def __init__(self, expr: list[Expression], body: list[Block], else_body: Block | None) -> None:
        super().__init__()
        self.expr = expr
        self.body = body
        self.else_body = else_body

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_if_stmt(self)


class RaiseStmt(Statement):
    __slots__ = ("expr", "from_expr")

    __match_args__ = ("expr", "from_expr")

    # Plain 'raise' is a valid statement.
    expr: Expression | None
    from_expr: Expression | None

    def __init__(self, expr: Expression | None, from_expr: Expression | None) -> None:
        super().__init__()
        self.expr = expr
        self.from_expr = from_expr

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_raise_stmt(self)


class TryStmt(Statement):
    __slots__ = ("body", "types", "vars", "handlers", "else_body", "finally_body", "is_star")

    __match_args__ = ("body", "types", "vars", "handlers", "else_body", "finally_body", "is_star")

    body: Block  # Try body
    # Plain 'except:' also possible
    types: list[Expression | None]  # Except type expressions
    vars: list[NameExpr | None]  # Except variable names
    handlers: list[Block]  # Except bodies
    else_body: Block | None
    finally_body: Block | None
    # Whether this is try ... except* (added in Python 3.11)
    is_star: bool

    def __init__(
        self,
        body: Block,
        vars: list[NameExpr | None],
        types: list[Expression | None],
        handlers: list[Block],
        else_body: Block | None,
        finally_body: Block | None,
    ) -> None:
        super().__init__()
        self.body = body
        self.vars = vars
        self.types = types
        self.handlers = handlers
        self.else_body = else_body
        self.finally_body = finally_body
        self.is_star = False

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_try_stmt(self)


class WithStmt(Statement):
    __slots__ = ("expr", "target", "unanalyzed_type", "analyzed_types", "body", "is_async")

    __match_args__ = ("expr", "target", "body")

    expr: list[Expression]
    target: list[Lvalue | None]
    # Type given by type comments for target, can be None
    unanalyzed_type: mypy.types.Type | None
    # Semantically analyzed types from type comment (TypeList type expanded)
    analyzed_types: list[mypy.types.Type]
    body: Block
    is_async: bool  # True if `async with ...` (PEP 492, Python 3.5)

    def __init__(
        self,
        expr: list[Expression],
        target: list[Lvalue | None],
        body: Block,
        target_type: mypy.types.Type | None = None,
    ) -> None:
        super().__init__()
        self.expr = expr
        self.target = target
        self.unanalyzed_type = target_type
        self.analyzed_types = []
        self.body = body
        self.is_async = False

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_with_stmt(self)


class MatchStmt(Statement):
    __slots__ = ("subject", "subject_dummy", "patterns", "guards", "bodies")

    __match_args__ = ("subject", "patterns", "guards", "bodies")

    subject: Expression
    subject_dummy: NameExpr | None
    patterns: list[Pattern]
    guards: list[Expression | None]
    bodies: list[Block]

    def __init__(
        self,
        subject: Expression,
        patterns: list[Pattern],
        guards: list[Expression | None],
        bodies: list[Block],
    ) -> None:
        super().__init__()
        assert len(patterns) == len(guards) == len(bodies)
        self.subject = subject
        self.subject_dummy = None
        self.patterns = patterns
        self.guards = guards
        self.bodies = bodies

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_match_stmt(self)


class TypeAliasStmt(Statement):
    __slots__ = ("name", "type_args", "value", "invalid_recursive_alias", "alias_node")

    __match_args__ = ("name", "type_args", "value")

    name: NameExpr
    type_args: list[TypeParam]
    value: LambdaExpr  # Return value will get translated into a type
    invalid_recursive_alias: bool
    alias_node: TypeAlias | None

    def __init__(self, name: NameExpr, type_args: list[TypeParam], value: LambdaExpr) -> None:
        super().__init__()
        self.name = name
        self.type_args = type_args
        self.value = value
        self.invalid_recursive_alias = False
        self.alias_node = None

    def accept(self, visitor: StatementVisitor[T]) -> T:
        return visitor.visit_type_alias_stmt(self)


# Expressions


class IntExpr(Expression):
    """Integer literal"""

    __slots__ = ("value",)

    __match_args__ = ("value",)

    value: int  # 0 by default

    def __init__(self, value: int) -> None:
        super().__init__()
        self.value = value

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_int_expr(self)


# How mypy uses StrExpr and BytesExpr:
#
# b'x' -> BytesExpr
# 'x', u'x' -> StrExpr


class StrExpr(Expression):
    """String literal"""

    __slots__ = ("value",)

    __match_args__ = ("value",)

    value: str  # '' by default

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_str_expr(self)


def is_StrExpr_list(seq: list[Expression]) -> TypeGuard[list[StrExpr]]:  # noqa: N802
    return all(isinstance(item, StrExpr) for item in seq)


class BytesExpr(Expression):
    """Bytes literal"""

    __slots__ = ("value",)

    __match_args__ = ("value",)

    # Note: we deliberately do NOT use bytes here because it ends up
    # unnecessarily complicating a lot of the result logic. For example,
    # we'd have to worry about converting the bytes into a format we can
    # easily serialize/deserialize to and from JSON, would have to worry
    # about turning the bytes into a human-readable representation in
    # error messages...
    #
    # It's more convenient to just store the human-readable representation
    # from the very start.
    value: str

    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_bytes_expr(self)


class FloatExpr(Expression):
    """Float literal"""

    __slots__ = ("value",)

    __match_args__ = ("value",)

    value: float  # 0.0 by default

    def __init__(self, value: float) -> None:
        super().__init__()
        self.value = value

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_float_expr(self)


class ComplexExpr(Expression):
    """Complex literal"""

    __slots__ = ("value",)

    __match_args__ = ("value",)

    value: complex

    def __init__(self, value: complex) -> None:
        super().__init__()
        self.value = value

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_complex_expr(self)


class EllipsisExpr(Expression):
    """Ellipsis (...)"""

    __slots__ = ()

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_ellipsis(self)


class StarExpr(Expression):
    """Star expression"""

    __slots__ = ("expr", "valid")

    __match_args__ = ("expr", "valid")

    expr: Expression
    valid: bool

    def __init__(self, expr: Expression) -> None:
        super().__init__()
        self.expr = expr

        # Whether this starred expression is used in a tuple/list and as lvalue
        self.valid = False

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_star_expr(self)


class RefExpr(Expression):
    """Abstract base class for name-like constructs"""

    __slots__ = (
        "kind",
        "node",
        "_fullname",
        "is_new_def",
        "is_inferred_def",
        "is_alias_rvalue",
        "type_guard",
        "type_is",
    )

    def __init__(self) -> None:
        super().__init__()
        # LDEF/GDEF/MDEF/... (None if not available)
        self.kind: int | None = None
        # Var, FuncDef or TypeInfo that describes this
        self.node: SymbolNode | None = None
        # Fully qualified name (or name if not global)
        self._fullname = ""
        # Does this define a new name?
        self.is_new_def = False
        # Does this define a new name with inferred type?
        #
        # For members, after semantic analysis, this does not take base
        # classes into consideration at all; the type checker deals with these.
        self.is_inferred_def = False
        # Is this expression appears as an rvalue of a valid type alias definition?
        self.is_alias_rvalue = False
        # Cache type guard from callable_type.type_guard
        self.type_guard: mypy.types.Type | None = None
        # And same for TypeIs
        self.type_is: mypy.types.Type | None = None

    @property
    def fullname(self) -> str:
        return self._fullname

    @fullname.setter
    def fullname(self, v: str) -> None:
        self._fullname = v


class NameExpr(RefExpr):
    """Name expression

    This refers to a local name, global name or a module.
    """

    __slots__ = ("name", "is_special_form")

    __match_args__ = ("name", "node")

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name  # Name referred to
        # Is this a l.h.s. of a special form assignment like typed dict or type variable?
        self.is_special_form = False

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_name_expr(self)

    def serialize(self) -> JsonDict:
        assert False, f"Serializing NameExpr: {self}"


class MemberExpr(RefExpr):
    """Member access expression x.y"""

    __slots__ = ("expr", "name", "def_var")

    __match_args__ = ("expr", "name", "node")

    def __init__(self, expr: Expression, name: str) -> None:
        super().__init__()
        self.expr = expr
        self.name = name
        # The variable node related to a definition through 'self.x = <initializer>'.
        # The nodes of other kinds of member expressions are resolved during type checking.
        self.def_var: Var | None = None

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_member_expr(self)


# Kinds of arguments
@unique
class ArgKind(Enum):
    # Positional argument
    ARG_POS = 0
    # Positional, optional argument (functions only, not calls)
    ARG_OPT = 1
    # *arg argument
    ARG_STAR = 2
    # Keyword argument x=y in call, or keyword-only function arg
    ARG_NAMED = 3
    # **arg argument
    ARG_STAR2 = 4
    # In an argument list, keyword-only and also optional
    ARG_NAMED_OPT = 5

    def is_positional(self, star: bool = False) -> bool:
        return self == ARG_POS or self == ARG_OPT or (star and self == ARG_STAR)

    def is_named(self, star: bool = False) -> bool:
        return self == ARG_NAMED or self == ARG_NAMED_OPT or (star and self == ARG_STAR2)

    def is_required(self) -> bool:
        return self == ARG_POS or self == ARG_NAMED

    def is_optional(self) -> bool:
        return self == ARG_OPT or self == ARG_NAMED_OPT

    def is_star(self) -> bool:
        return self == ARG_STAR or self == ARG_STAR2


ARG_POS: Final = ArgKind.ARG_POS
ARG_OPT: Final = ArgKind.ARG_OPT
ARG_STAR: Final = ArgKind.ARG_STAR
ARG_NAMED: Final = ArgKind.ARG_NAMED
ARG_STAR2: Final = ArgKind.ARG_STAR2
ARG_NAMED_OPT: Final = ArgKind.ARG_NAMED_OPT


class CallExpr(Expression):
    """Call expression.

    This can also represent several special forms that are syntactically calls
    such as cast(...) and None  # type: ....
    """

    __slots__ = ("callee", "args", "arg_kinds", "arg_names", "analyzed")

    __match_args__ = ("callee", "args", "arg_kinds", "arg_names")

    def __init__(
        self,
        callee: Expression,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        arg_names: list[str | None],
        analyzed: Expression | None = None,
    ) -> None:
        super().__init__()
        if not arg_names:
            arg_names = [None] * len(args)

        self.callee = callee
        self.args = args
        self.arg_kinds = arg_kinds  # ARG_ constants
        # Each name can be None if not a keyword argument.
        self.arg_names: list[str | None] = arg_names
        # If not None, the node that represents the meaning of the CallExpr. For
        # cast(...) this is a CastExpr.
        self.analyzed = analyzed

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_call_expr(self)


class YieldFromExpr(Expression):
    __slots__ = ("expr",)

    __match_args__ = ("expr",)

    expr: Expression

    def __init__(self, expr: Expression) -> None:
        super().__init__()
        self.expr = expr

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_yield_from_expr(self)


class YieldExpr(Expression):
    __slots__ = ("expr",)

    __match_args__ = ("expr",)

    expr: Expression | None

    def __init__(self, expr: Expression | None) -> None:
        super().__init__()
        self.expr = expr

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_yield_expr(self)


class IndexExpr(Expression):
    """Index expression x[y].

    Also wraps type application such as List[int] as a special form.
    """

    __slots__ = ("base", "index", "method_type", "analyzed")

    __match_args__ = ("base", "index")

    base: Expression
    index: Expression
    # Inferred __getitem__ method type
    method_type: mypy.types.Type | None
    # If not None, this is actually semantically a type application
    # Class[type, ...] or a type alias initializer.
    analyzed: TypeApplication | TypeAliasExpr | None

    def __init__(self, base: Expression, index: Expression) -> None:
        super().__init__()
        self.base = base
        self.index = index
        self.method_type = None
        self.analyzed = None

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_index_expr(self)


class UnaryExpr(Expression):
    """Unary operation"""

    __slots__ = ("op", "expr", "method_type")

    __match_args__ = ("op", "expr")

    op: str  # TODO: Enum?
    expr: Expression
    # Inferred operator method type
    method_type: mypy.types.Type | None

    def __init__(self, op: str, expr: Expression) -> None:
        super().__init__()
        self.op = op
        self.expr = expr
        self.method_type = None

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_unary_expr(self)


class AssignmentExpr(Expression):
    """Assignment expressions in Python 3.8+, like "a := 2"."""

    __slots__ = ("target", "value")

    __match_args__ = ("target", "value")

    def __init__(self, target: NameExpr, value: Expression) -> None:
        super().__init__()
        self.target = target
        self.value = value

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_assignment_expr(self)


class OpExpr(Expression):
    """Binary operation.

    The dot (.), [] and comparison operators have more specific nodes.
    """

    __slots__ = (
        "op",
        "left",
        "right",
        "method_type",
        "right_always",
        "right_unreachable",
        "analyzed",
    )

    __match_args__ = ("left", "op", "right")

    op: str  # TODO: Enum?
    left: Expression
    right: Expression
    # Inferred type for the operator method type (when relevant).
    method_type: mypy.types.Type | None
    # Per static analysis only: Is the right side going to be evaluated every time?
    right_always: bool
    # Per static analysis only: Is the right side unreachable?
    right_unreachable: bool
    # Used for expressions that represent a type "X | Y" in some contexts
    analyzed: TypeAliasExpr | None

    def __init__(
        self, op: str, left: Expression, right: Expression, analyzed: TypeAliasExpr | None = None
    ) -> None:
        super().__init__()
        self.op = op
        self.left = left
        self.right = right
        self.method_type = None
        self.right_always = False
        self.right_unreachable = False
        self.analyzed = analyzed

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_op_expr(self)


class ComparisonExpr(Expression):
    """Comparison expression (e.g. a < b > c < d)."""

    __slots__ = ("operators", "operands", "method_types")

    __match_args__ = ("operands", "operators")

    operators: list[str]
    operands: list[Expression]
    # Inferred type for the operator methods (when relevant; None for 'is').
    method_types: list[mypy.types.Type | None]

    def __init__(self, operators: list[str], operands: list[Expression]) -> None:
        super().__init__()
        self.operators = operators
        self.operands = operands
        self.method_types = []

    def pairwise(self) -> Iterator[tuple[str, Expression, Expression]]:
        """If this comparison expr is "a < b is c == d", yields the sequence
        ("<", a, b), ("is", b, c), ("==", c, d)
        """
        for i, operator in enumerate(self.operators):
            yield operator, self.operands[i], self.operands[i + 1]

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_comparison_expr(self)


class SliceExpr(Expression):
    """Slice expression (e.g. 'x:y', 'x:', '::2' or ':').

    This is only valid as index in index expressions.
    """

    __slots__ = ("begin_index", "end_index", "stride")

    __match_args__ = ("begin_index", "end_index", "stride")

    begin_index: Expression | None
    end_index: Expression | None
    stride: Expression | None

    def __init__(
        self,
        begin_index: Expression | None,
        end_index: Expression | None,
        stride: Expression | None,
    ) -> None:
        super().__init__()
        self.begin_index = begin_index
        self.end_index = end_index
        self.stride = stride

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_slice_expr(self)


class CastExpr(Expression):
    """Cast expression cast(type, expr)."""

    __slots__ = ("expr", "type")

    __match_args__ = ("expr", "type")

    expr: Expression
    type: mypy.types.Type

    def __init__(self, expr: Expression, typ: mypy.types.Type) -> None:
        super().__init__()
        self.expr = expr
        self.type = typ

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_cast_expr(self)


class AssertTypeExpr(Expression):
    """Represents a typing.assert_type(expr, type) call."""

    __slots__ = ("expr", "type")

    __match_args__ = ("expr", "type")

    expr: Expression
    type: mypy.types.Type

    def __init__(self, expr: Expression, typ: mypy.types.Type) -> None:
        super().__init__()
        self.expr = expr
        self.type = typ

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_assert_type_expr(self)


class RevealExpr(Expression):
    """Reveal type expression reveal_type(expr) or reveal_locals() expression."""

    __slots__ = ("expr", "kind", "local_nodes", "is_imported")

    __match_args__ = ("expr", "kind", "local_nodes", "is_imported")

    expr: Expression | None
    kind: int
    local_nodes: list[Var] | None

    def __init__(
        self,
        kind: int,
        expr: Expression | None = None,
        local_nodes: list[Var] | None = None,
        is_imported: bool = False,
    ) -> None:
        super().__init__()
        self.expr = expr
        self.kind = kind
        self.local_nodes = local_nodes
        self.is_imported = is_imported

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_reveal_expr(self)


class SuperExpr(Expression):
    """Expression super().name"""

    __slots__ = ("name", "info", "call")

    __match_args__ = ("name", "call", "info")

    name: str
    info: TypeInfo | None  # Type that contains this super expression
    call: CallExpr  # The expression super(...)

    def __init__(self, name: str, call: CallExpr) -> None:
        super().__init__()
        self.name = name
        self.call = call
        self.info = None

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_super_expr(self)


class LambdaExpr(FuncItem, Expression):
    """Lambda expression"""

    __match_args__ = ("arguments", "arg_names", "arg_kinds", "body")

    @property
    def name(self) -> str:
        return LAMBDA_NAME

    def expr(self) -> Expression:
        """Return the expression (the body) of the lambda."""
        ret = self.body.body[-1]
        assert isinstance(ret, ReturnStmt)
        expr = ret.expr
        assert expr is not None  # lambda can't have empty body
        return expr

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_lambda_expr(self)

    def is_dynamic(self) -> bool:
        return False


class ListExpr(Expression):
    """List literal expression [...]."""

    __slots__ = ("items",)

    __match_args__ = ("items",)

    items: list[Expression]

    def __init__(self, items: list[Expression]) -> None:
        super().__init__()
        self.items = items

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_list_expr(self)


class DictExpr(Expression):
    """Dictionary literal expression {key: value, ...}."""

    __slots__ = ("items",)

    __match_args__ = ("items",)

    items: list[tuple[Expression | None, Expression]]

    def __init__(self, items: list[tuple[Expression | None, Expression]]) -> None:
        super().__init__()
        self.items = items

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_dict_expr(self)


class TupleExpr(Expression):
    """Tuple literal expression (..., ...)

    Also lvalue sequences (..., ...) and [..., ...]"""

    __slots__ = ("items",)

    __match_args__ = ("items",)

    items: list[Expression]

    def __init__(self, items: list[Expression]) -> None:
        super().__init__()
        self.items = items

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_tuple_expr(self)


class SetExpr(Expression):
    """Set literal expression {value, ...}."""

    __slots__ = ("items",)

    __match_args__ = ("items",)

    items: list[Expression]

    def __init__(self, items: list[Expression]) -> None:
        super().__init__()
        self.items = items

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_set_expr(self)


class GeneratorExpr(Expression):
    """Generator expression ... for ... in ... [ for ...  in ... ] [ if ... ]."""

    __slots__ = ("left_expr", "sequences", "condlists", "is_async", "indices")

    __match_args__ = ("left_expr", "indices", "sequences", "condlists")

    left_expr: Expression
    sequences: list[Expression]
    condlists: list[list[Expression]]
    is_async: list[bool]
    indices: list[Lvalue]

    def __init__(
        self,
        left_expr: Expression,
        indices: list[Lvalue],
        sequences: list[Expression],
        condlists: list[list[Expression]],
        is_async: list[bool],
    ) -> None:
        super().__init__()
        self.left_expr = left_expr
        self.sequences = sequences
        self.condlists = condlists
        self.indices = indices
        self.is_async = is_async

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_generator_expr(self)


class ListComprehension(Expression):
    """List comprehension (e.g. [x + 1 for x in a])"""

    __slots__ = ("generator",)

    __match_args__ = ("generator",)

    generator: GeneratorExpr

    def __init__(self, generator: GeneratorExpr) -> None:
        super().__init__()
        self.generator = generator

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_list_comprehension(self)


class SetComprehension(Expression):
    """Set comprehension (e.g. {x + 1 for x in a})"""

    __slots__ = ("generator",)

    __match_args__ = ("generator",)

    generator: GeneratorExpr

    def __init__(self, generator: GeneratorExpr) -> None:
        super().__init__()
        self.generator = generator

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_set_comprehension(self)


class DictionaryComprehension(Expression):
    """Dictionary comprehension (e.g. {k: v for k, v in a}"""

    __slots__ = ("key", "value", "sequences", "condlists", "is_async", "indices")

    __match_args__ = ("key", "value", "indices", "sequences", "condlists")

    key: Expression
    value: Expression
    sequences: list[Expression]
    condlists: list[list[Expression]]
    is_async: list[bool]
    indices: list[Lvalue]

    def __init__(
        self,
        key: Expression,
        value: Expression,
        indices: list[Lvalue],
        sequences: list[Expression],
        condlists: list[list[Expression]],
        is_async: list[bool],
    ) -> None:
        super().__init__()
        self.key = key
        self.value = value
        self.sequences = sequences
        self.condlists = condlists
        self.indices = indices
        self.is_async = is_async

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_dictionary_comprehension(self)


class ConditionalExpr(Expression):
    """Conditional expression (e.g. x if y else z)"""

    __slots__ = ("cond", "if_expr", "else_expr")

    __match_args__ = ("if_expr", "cond", "else_expr")

    cond: Expression
    if_expr: Expression
    else_expr: Expression

    def __init__(self, cond: Expression, if_expr: Expression, else_expr: Expression) -> None:
        super().__init__()
        self.cond = cond
        self.if_expr = if_expr
        self.else_expr = else_expr

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_conditional_expr(self)


class TypeApplication(Expression):
    """Type application expr[type, ...]"""

    __slots__ = ("expr", "types")

    __match_args__ = ("expr", "types")

    expr: Expression
    types: list[mypy.types.Type]

    def __init__(self, expr: Expression, types: list[mypy.types.Type]) -> None:
        super().__init__()
        self.expr = expr
        self.types = types

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_type_application(self)


# Variance of a type variable. For example, T in the definition of
# List[T] is invariant, so List[int] is not a subtype of List[object],
# and also List[object] is not a subtype of List[int].
#
# The T in Iterable[T] is covariant, so Iterable[int] is a subtype of
# Iterable[object], but not vice versa.
#
# If T is contravariant in Foo[T], Foo[object] is a subtype of
# Foo[int], but not vice versa.
INVARIANT: Final = 0
COVARIANT: Final = 1
CONTRAVARIANT: Final = 2
VARIANCE_NOT_READY: Final = 3  # Variance hasn't been inferred (using Python 3.12 syntax)


class TypeVarLikeExpr(SymbolNode, Expression):
    """Base class for TypeVarExpr, ParamSpecExpr and TypeVarTupleExpr.

    Note that they are constructed by the semantic analyzer.
    """

    __slots__ = ("_name", "_fullname", "upper_bound", "default", "variance", "is_new_style")

    _name: str
    _fullname: str
    # Upper bound: only subtypes of upper_bound are valid as values. By default
    # this is 'object', meaning no restriction.
    upper_bound: mypy.types.Type
    # Default: used to resolve the TypeVar if the default is not explicitly given.
    # By default this is 'AnyType(TypeOfAny.from_omitted_generics)'. See PEP 696.
    default: mypy.types.Type
    # Variance of the type variable. Invariant is the default.
    # TypeVar(..., covariant=True) defines a covariant type variable.
    # TypeVar(..., contravariant=True) defines a contravariant type
    # variable.
    variance: int

    def __init__(
        self,
        name: str,
        fullname: str,
        upper_bound: mypy.types.Type,
        default: mypy.types.Type,
        variance: int = INVARIANT,
        is_new_style: bool = False,
        line: int = -1,
    ) -> None:
        super().__init__(line=line)
        self._name = name
        self._fullname = fullname
        self.upper_bound = upper_bound
        self.default = default
        self.variance = variance
        self.is_new_style = is_new_style

    @property
    def name(self) -> str:
        return self._name

    @property
    def fullname(self) -> str:
        return self._fullname


# All types that are both SymbolNodes and Expressions.
# Use when common children of them are needed.
SYMBOL_NODE_EXPRESSION_TYPES: Final = (TypeVarLikeExpr,)


class TypeVarExpr(TypeVarLikeExpr):
    """Type variable expression TypeVar(...).

    This is also used to represent type variables in symbol tables.

    A type variable is not valid as a type unless bound in a TypeVarLikeScope.
    That happens within:

     1. a generic class that uses the type variable as a type argument or
     2. a generic function that refers to the type variable in its signature.
    """

    __slots__ = ("values",)

    __match_args__ = ("name", "values", "upper_bound", "default")

    # Value restriction: only types in the list are valid as values. If the
    # list is empty, there is no restriction.
    values: list[mypy.types.Type]

    def __init__(
        self,
        name: str,
        fullname: str,
        values: list[mypy.types.Type],
        upper_bound: mypy.types.Type,
        default: mypy.types.Type,
        variance: int = INVARIANT,
        is_new_style: bool = False,
        line: int = -1,
    ) -> None:
        super().__init__(name, fullname, upper_bound, default, variance, is_new_style, line=line)
        self.values = values

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_type_var_expr(self)

    def serialize(self) -> JsonDict:
        return {
            ".class": "TypeVarExpr",
            "name": self._name,
            "fullname": self._fullname,
            "values": [t.serialize() for t in self.values],
            "upper_bound": self.upper_bound.serialize(),
            "default": self.default.serialize(),
            "variance": self.variance,
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> TypeVarExpr:
        assert data[".class"] == "TypeVarExpr"
        return TypeVarExpr(
            data["name"],
            data["fullname"],
            [mypy.types.deserialize_type(v) for v in data["values"]],
            mypy.types.deserialize_type(data["upper_bound"]),
            mypy.types.deserialize_type(data["default"]),
            data["variance"],
        )


class ParamSpecExpr(TypeVarLikeExpr):
    __slots__ = ()

    __match_args__ = ("name", "upper_bound", "default")

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_paramspec_expr(self)

    def serialize(self) -> JsonDict:
        return {
            ".class": "ParamSpecExpr",
            "name": self._name,
            "fullname": self._fullname,
            "upper_bound": self.upper_bound.serialize(),
            "default": self.default.serialize(),
            "variance": self.variance,
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> ParamSpecExpr:
        assert data[".class"] == "ParamSpecExpr"
        return ParamSpecExpr(
            data["name"],
            data["fullname"],
            mypy.types.deserialize_type(data["upper_bound"]),
            mypy.types.deserialize_type(data["default"]),
            data["variance"],
        )


class TypeVarTupleExpr(TypeVarLikeExpr):
    """Type variable tuple expression TypeVarTuple(...)."""

    __slots__ = "tuple_fallback"

    tuple_fallback: mypy.types.Instance

    __match_args__ = ("name", "upper_bound", "default")

    def __init__(
        self,
        name: str,
        fullname: str,
        upper_bound: mypy.types.Type,
        tuple_fallback: mypy.types.Instance,
        default: mypy.types.Type,
        variance: int = INVARIANT,
        is_new_style: bool = False,
        line: int = -1,
    ) -> None:
        super().__init__(name, fullname, upper_bound, default, variance, is_new_style, line=line)
        self.tuple_fallback = tuple_fallback

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_type_var_tuple_expr(self)

    def serialize(self) -> JsonDict:
        return {
            ".class": "TypeVarTupleExpr",
            "name": self._name,
            "fullname": self._fullname,
            "upper_bound": self.upper_bound.serialize(),
            "tuple_fallback": self.tuple_fallback.serialize(),
            "default": self.default.serialize(),
            "variance": self.variance,
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> TypeVarTupleExpr:
        assert data[".class"] == "TypeVarTupleExpr"
        return TypeVarTupleExpr(
            data["name"],
            data["fullname"],
            mypy.types.deserialize_type(data["upper_bound"]),
            mypy.types.Instance.deserialize(data["tuple_fallback"]),
            mypy.types.deserialize_type(data["default"]),
            data["variance"],
        )


class TypeAliasExpr(Expression):
    """Type alias expression (rvalue)."""

    __slots__ = ("node",)

    __match_args__ = ("node",)

    node: TypeAlias

    def __init__(self, node: TypeAlias) -> None:
        super().__init__()
        self.node = node

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_type_alias_expr(self)


class NamedTupleExpr(Expression):
    """Named tuple expression namedtuple(...) or NamedTuple(...)."""

    __slots__ = ("info", "is_typed")

    __match_args__ = ("info",)

    # The class representation of this named tuple (its tuple_type attribute contains
    # the tuple item types)
    info: TypeInfo
    is_typed: bool  # whether this class was created with typing(_extensions).NamedTuple

    def __init__(self, info: TypeInfo, is_typed: bool = False) -> None:
        super().__init__()
        self.info = info
        self.is_typed = is_typed

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_namedtuple_expr(self)


class TypedDictExpr(Expression):
    """Typed dict expression TypedDict(...)."""

    __slots__ = ("info",)

    __match_args__ = ("info",)

    # The class representation of this typed dict
    info: TypeInfo

    def __init__(self, info: TypeInfo) -> None:
        super().__init__()
        self.info = info

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_typeddict_expr(self)


class EnumCallExpr(Expression):
    """Named tuple expression Enum('name', 'val1 val2 ...')."""

    __slots__ = ("info", "items", "values")

    __match_args__ = ("info", "items", "values")

    # The class representation of this enumerated type
    info: TypeInfo
    # The item names (for debugging)
    items: list[str]
    values: list[Expression | None]

    def __init__(self, info: TypeInfo, items: list[str], values: list[Expression | None]) -> None:
        super().__init__()
        self.info = info
        self.items = items
        self.values = values

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_enum_call_expr(self)


class PromoteExpr(Expression):
    """Ducktype class decorator expression _promote(...)."""

    __slots__ = ("type",)

    type: mypy.types.ProperType

    def __init__(self, type: mypy.types.ProperType) -> None:
        super().__init__()
        self.type = type

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit__promote_expr(self)


class NewTypeExpr(Expression):
    """NewType expression NewType(...)."""

    __slots__ = ("name", "old_type", "info")

    __match_args__ = ("name", "old_type", "info")

    name: str
    # The base type (the second argument to NewType)
    old_type: mypy.types.Type | None
    # The synthesized class representing the new type (inherits old_type)
    info: TypeInfo | None

    def __init__(
        self, name: str, old_type: mypy.types.Type | None, line: int, column: int
    ) -> None:
        super().__init__(line=line, column=column)
        self.name = name
        self.old_type = old_type
        self.info = None

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_newtype_expr(self)


class AwaitExpr(Expression):
    """Await expression (await ...)."""

    __slots__ = ("expr",)

    __match_args__ = ("expr",)

    expr: Expression

    def __init__(self, expr: Expression) -> None:
        super().__init__()
        self.expr = expr

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_await_expr(self)


# Constants


class TempNode(Expression):
    """Temporary dummy node used during type checking.

    This node is not present in the original program; it is just an artifact
    of the type checker implementation. It only represents an opaque node with
    some fixed type.
    """

    __slots__ = ("type", "no_rhs")

    type: mypy.types.Type
    # Is this TempNode used to indicate absence of a right hand side in an annotated assignment?
    # (e.g. for 'x: int' the rvalue is TempNode(AnyType(TypeOfAny.special_form), no_rhs=True))
    no_rhs: bool

    def __init__(
        self, typ: mypy.types.Type, no_rhs: bool = False, *, context: Context | None = None
    ) -> None:
        """Construct a dummy node; optionally borrow line/column from context object."""
        super().__init__()
        self.type = typ
        self.no_rhs = no_rhs
        if context is not None:
            self.line = context.line
            self.column = context.column

    def __repr__(self) -> str:
        return "TempNode:%d(%s)" % (self.line, str(self.type))

    def accept(self, visitor: ExpressionVisitor[T]) -> T:
        return visitor.visit_temp_node(self)


# Special attributes not collected as protocol members by Python 3.12
# See typing._SPECIAL_NAMES
EXCLUDED_PROTOCOL_ATTRIBUTES: Final = frozenset(
    {
        "__abstractmethods__",
        "__annotations__",
        "__dict__",
        "__doc__",
        "__init__",
        "__module__",
        "__new__",
        "__slots__",
        "__subclasshook__",
        "__weakref__",
        "__class_getitem__",  # Since Python 3.9
    }
)

# Attributes that can optionally be defined in the body of a subclass of
# enum.Enum but are removed from the class __dict__ by EnumMeta.
EXCLUDED_ENUM_ATTRIBUTES: Final = frozenset({"_ignore_", "_order_", "__order__"})


class TypeInfo(SymbolNode):
    """The type structure of a single class.

    Each TypeInfo corresponds one-to-one to a ClassDef, which
    represents the AST of the class.

    In type-theory terms, this is a "type constructor", and if the
    class is generic then it will be a type constructor of higher kind.
    Where the class is used in an actual type, it's in the form of an
    Instance, which amounts to a type application of the tycon to
    the appropriate number of arguments.
    """

    __slots__ = (
        "_fullname",
        "module_name",
        "defn",
        "mro",
        "_mro_refs",
        "bad_mro",
        "is_final",
        "declared_metaclass",
        "metaclass_type",
        "names",
        "is_abstract",
        "is_protocol",
        "runtime_protocol",
        "abstract_attributes",
        "deletable_attributes",
        "slots",
        "assuming",
        "assuming_proper",
        "inferring",
        "is_enum",
        "fallback_to_any",
        "meta_fallback_to_any",
        "type_vars",
        "has_param_spec_type",
        "bases",
        "_promote",
        "tuple_type",
        "special_alias",
        "is_named_tuple",
        "typeddict_type",
        "is_newtype",
        "is_intersection",
        "metadata",
        "alt_promote",
        "has_type_var_tuple_type",
        "type_var_tuple_prefix",
        "type_var_tuple_suffix",
        "self_type",
        "dataclass_transform_spec",
        "is_type_check_only",
        "deprecated",
        "type_object_type",
    )

    _fullname: str  # Fully qualified name
    # Fully qualified name for the module this type was defined in. This
    # information is also in the fullname, but is harder to extract in the
    # case of nested class definitions.
    module_name: str
    defn: ClassDef  # Corresponding ClassDef
    # Method Resolution Order: the order of looking up attributes. The first
    # value always to refers to this class.
    mro: list[TypeInfo]
    # Used to stash the names of the mro classes temporarily between
    # deserialization and fixup. See deserialize() for why.
    _mro_refs: list[str] | None
    bad_mro: bool  # Could not construct full MRO
    is_final: bool

    declared_metaclass: mypy.types.Instance | None
    metaclass_type: mypy.types.Instance | None

    names: SymbolTable  # Names defined directly in this type
    is_abstract: bool  # Does the class have any abstract attributes?
    is_protocol: bool  # Is this a protocol class?
    runtime_protocol: bool  # Does this protocol support isinstance checks?
    # List of names of abstract attributes together with their abstract status.
    # The abstract status must be one of `NOT_ABSTRACT`, `IS_ABSTRACT`, `IMPLICITLY_ABSTRACT`.
    abstract_attributes: list[tuple[str, int]]
    deletable_attributes: list[str]  # Used by mypyc only
    # Does this type have concrete `__slots__` defined?
    # If class does not have `__slots__` defined then it is `None`,
    # if it has empty `__slots__` then it is an empty set.
    slots: set[str] | None

    # The attributes 'assuming' and 'assuming_proper' represent structural subtype matrices.
    #
    # In languages with structural subtyping, one can keep a global subtype matrix like this:
    #   . A B C .
    #   A 1 0 0
    #   B 1 1 1
    #   C 1 0 1
    #   .
    # where 1 indicates that the type in corresponding row is a subtype of the type
    # in corresponding column. This matrix typically starts filled with all 1's and
    # a typechecker tries to "disprove" every subtyping relation using atomic (or nominal) types.
    # However, we don't want to keep this huge global state. Instead, we keep the subtype
    # information in the form of list of pairs (subtype, supertype) shared by all Instances
    # with given supertype's TypeInfo. When we enter a subtype check we push a pair in this list
    # thus assuming that we started with 1 in corresponding matrix element. Such algorithm allows
    # to treat recursive and mutually recursive protocols and other kinds of complex situations.
    #
    # If concurrent/parallel type checking will be added in future,
    # then there should be one matrix per thread/process to avoid false negatives
    # during the type checking phase.
    assuming: list[tuple[mypy.types.Instance, mypy.types.Instance]]
    assuming_proper: list[tuple[mypy.types.Instance, mypy.types.Instance]]
    # Ditto for temporary 'inferring' stack of recursive constraint inference.
    # It contains Instances of protocol types that appeared as an argument to
    # constraints.infer_constraints(). We need 'inferring' to avoid infinite recursion for
    # recursive and mutually recursive protocols.
    #
    # We make 'assuming' and 'inferring' attributes here instead of passing they as kwargs,
    # since this would require to pass them in many dozens of calls. In particular,
    # there is a dependency infer_constraint -> is_subtype -> is_callable_subtype ->
    # -> infer_constraints.
    inferring: list[mypy.types.Instance]
    # 'inferring' and 'assuming' can't be made sets, since we need to use
    # is_same_type to correctly treat unions.

    # Classes inheriting from Enum shadow their true members with a __getattr__, so we
    # have to treat them as a special case.
    is_enum: bool
    # If true, any unknown attributes should have type 'Any' instead
    # of generating a type error.  This would be true if there is a
    # base class with type 'Any', but other use cases may be
    # possible. This is similar to having __getattr__ that returns Any
    # (and __setattr__), but without the __getattr__ method.
    fallback_to_any: bool

    # Same as above but for cases where metaclass has type Any. This will suppress
    # all attribute errors only for *class object* access.
    meta_fallback_to_any: bool

    # Information related to type annotations.

    # Generic type variable names (full names)
    type_vars: list[str]

    # Whether this class has a ParamSpec type variable
    has_param_spec_type: bool

    # Direct base classes.
    bases: list[mypy.types.Instance]

    # Another type which this type will be treated as a subtype of,
    # even though it's not a subclass in Python.  The non-standard
    # `@_promote` decorator introduces this, and there are also
    # several builtin examples, in particular `int` -> `float`.
    _promote: list[mypy.types.ProperType]

    # This is used for promoting native integer types such as 'i64' to
    # 'int'. (_promote is used for the other direction.) This only
    # supports one-step promotions (e.g., i64 -> int, not
    # i64 -> int -> float, and this isn't used to promote in joins.
    #
    # This results in some unintuitive results, such as that even
    # though i64 is compatible with int and int is compatible with
    # float, i64 is *not* compatible with float.
    alt_promote: mypy.types.Instance | None

    # Representation of a Tuple[...] base class, if the class has any
    # (e.g., for named tuples). If this is not None, the actual Type
    # object used for this class is not an Instance but a TupleType;
    # the corresponding Instance is set as the fallback type of the
    # tuple type.
    tuple_type: mypy.types.TupleType | None

    # Is this a named tuple type?
    is_named_tuple: bool

    # If this class is defined by the TypedDict type constructor,
    # then this is not None.
    typeddict_type: mypy.types.TypedDictType | None

    # Is this a newtype type?
    is_newtype: bool

    # Is this a synthesized intersection type?
    is_intersection: bool

    # This is a dictionary that will be serialized and un-serialized as is.
    # It is useful for plugins to add their data to save in the cache.
    metadata: dict[str, JsonDict]

    # Store type alias representing this type (for named tuples and TypedDicts).
    # Although definitions of these types are stored in symbol tables as TypeInfo,
    # when a type analyzer will find them, it should construct a TupleType, or
    # a TypedDict type. However, we can't use the plain types, since if the definition
    # is recursive, this will create an actual recursive structure of types (i.e. as
    # internal Python objects) causing infinite recursions everywhere during type checking.
    # To overcome this, we create a TypeAlias node, that will point to these types.
    # We store this node in the `special_alias` attribute, because it must be the same node
    # in case we are doing multiple semantic analysis passes.
    special_alias: TypeAlias | None

    # Shared type variable for typing.Self in this class (if used, otherwise None).
    self_type: mypy.types.TypeVarType | None

    # Added if the corresponding class is directly decorated with `typing.dataclass_transform`
    dataclass_transform_spec: DataclassTransformSpec | None

    # Is set to `True` when class is decorated with `@typing.type_check_only`
    is_type_check_only: bool

    # The type's deprecation message (in case it is deprecated)
    deprecated: str | None

    # Cached value of class constructor type, i.e. the type of class object when it
    # appears in runtime context.
    type_object_type: mypy.types.FunctionLike | None

    FLAGS: Final = [
        "is_abstract",
        "is_enum",
        "fallback_to_any",
        "meta_fallback_to_any",
        "is_named_tuple",
        "is_newtype",
        "is_protocol",
        "runtime_protocol",
        "is_final",
        "is_intersection",
    ]

    def __init__(self, names: SymbolTable, defn: ClassDef, module_name: str) -> None:
        """Initialize a TypeInfo."""
        super().__init__()
        self._fullname = defn.fullname
        self.names = names
        self.defn = defn
        self.module_name = module_name
        self.type_vars = []
        self.has_param_spec_type = False
        self.has_type_var_tuple_type = False
        self.bases = []
        self.mro = []
        self._mro_refs = None
        self.bad_mro = False
        self.declared_metaclass = None
        self.metaclass_type = None
        self.is_abstract = False
        self.abstract_attributes = []
        self.deletable_attributes = []
        self.slots = None
        self.assuming = []
        self.assuming_proper = []
        self.inferring = []
        self.is_protocol = False
        self.runtime_protocol = False
        self.type_var_tuple_prefix: int | None = None
        self.type_var_tuple_suffix: int | None = None
        self.add_type_vars()
        self.is_final = False
        self.is_enum = False
        self.fallback_to_any = False
        self.meta_fallback_to_any = False
        self._promote = []
        self.alt_promote = None
        self.tuple_type = None
        self.special_alias = None
        self.is_named_tuple = False
        self.typeddict_type = None
        self.is_newtype = False
        self.is_intersection = False
        self.metadata = {}
        self.self_type = None
        self.dataclass_transform_spec = None
        self.is_type_check_only = False
        self.deprecated = None
        self.type_object_type = None

    def add_type_vars(self) -> None:
        self.has_type_var_tuple_type = False
        if self.defn.type_vars:
            for i, vd in enumerate(self.defn.type_vars):
                if isinstance(vd, mypy.types.ParamSpecType):
                    self.has_param_spec_type = True
                if isinstance(vd, mypy.types.TypeVarTupleType):
                    assert not self.has_type_var_tuple_type
                    self.has_type_var_tuple_type = True
                    self.type_var_tuple_prefix = i
                    self.type_var_tuple_suffix = len(self.defn.type_vars) - i - 1
                self.type_vars.append(vd.name)

    @property
    def name(self) -> str:
        """Short name."""
        return self.defn.name

    @property
    def fullname(self) -> str:
        return self._fullname

    def is_generic(self) -> bool:
        """Is the type generic (i.e. does it have type variables)?"""
        return len(self.type_vars) > 0

    def get(self, name: str) -> SymbolTableNode | None:
        for cls in self.mro:
            n = cls.names.get(name)
            if n:
                return n
        return None

    def get_containing_type_info(self, name: str) -> TypeInfo | None:
        for cls in self.mro:
            if name in cls.names:
                return cls
        return None

    @property
    def protocol_members(self) -> list[str]:
        # Protocol members are names of all attributes/methods defined in a protocol
        # and in all its supertypes (except for 'object').
        members: set[str] = set()
        assert self.mro, "This property can be only accessed after MRO is (re-)calculated"
        for base in self.mro[:-1]:  # we skip "object" since everyone implements it
            if base.is_protocol:
                for name, node in base.names.items():
                    if isinstance(node.node, (TypeAlias, TypeVarExpr, MypyFile)):
                        # These are auxiliary definitions (and type aliases are prohibited).
                        continue
                    if name in EXCLUDED_PROTOCOL_ATTRIBUTES:
                        continue
                    members.add(name)
        return sorted(members)

    @property
    def enum_members(self) -> list[str]:
        # TODO: cache the results?
        members = []
        for name, sym in self.names.items():
            # Case 1:
            #
            # class MyEnum(Enum):
            #     @member
            #     def some(self): ...
            if isinstance(sym.node, Decorator):
                if any(
                    dec.fullname == "enum.member"
                    for dec in sym.node.decorators
                    if isinstance(dec, RefExpr)
                ):
                    members.append(name)
                    continue
            # Case 2:
            #
            # class MyEnum(Enum):
            #     x = 1
            #
            # Case 3:
            #
            # class MyEnum(Enum):
            #     class Other: ...
            elif isinstance(sym.node, (Var, TypeInfo)):
                if (
                    # TODO: properly support ignored names from `_ignore_`
                    name in EXCLUDED_ENUM_ATTRIBUTES
                    or is_sunder(name)
                    or name.startswith("__")  # dunder and private
                ):
                    continue  # name is excluded

                if isinstance(sym.node, Var):
                    if not sym.node.has_explicit_value:
                        continue  # unannotated value not a member

                    typ = mypy.types.get_proper_type(sym.node.type)
                    if (
                        isinstance(typ, mypy.types.FunctionLike) and not typ.is_type_obj()
                    ) or (  # explicit `@member` is required
                        isinstance(typ, mypy.types.Instance)
                        and typ.type.fullname == "enum.nonmember"
                    ):
                        continue  # name is not a member

                members.append(name)
        return members

    def __getitem__(self, name: str) -> SymbolTableNode:
        n = self.get(name)
        if n:
            return n
        else:
            raise KeyError(name)

    def __repr__(self) -> str:
        return f"<TypeInfo {self.fullname}>"

    def __bool__(self) -> bool:
        # We defined this here instead of just overriding it in
        # FakeInfo so that mypyc can generate a direct call instead of
        # using the generic bool handling.
        return not isinstance(self, FakeInfo)

    def has_readable_member(self, name: str) -> bool:
        return self.get(name) is not None

    def get_method(self, name: str) -> FuncBase | Decorator | None:
        for cls in self.mro:
            if name in cls.names:
                node = cls.names[name].node
                if isinstance(node, SYMBOL_FUNCBASE_TYPES):
                    return node
                elif isinstance(node, Decorator):  # Two `if`s make `mypyc` happy
                    return node
                else:
                    return None
        return None

    def calculate_metaclass_type(self) -> mypy.types.Instance | None:
        declared = self.declared_metaclass
        if declared is not None and not declared.type.has_base("builtins.type"):
            return declared
        if self._fullname == "builtins.type":
            return mypy.types.Instance(self, [])

        winner = declared
        for super_class in self.mro[1:]:
            super_meta = super_class.declared_metaclass
            if super_meta is None or super_meta.type is None:
                continue
            if winner is None:
                winner = super_meta
                continue
            if winner.type.has_base(super_meta.type.fullname):
                continue
            if super_meta.type.has_base(winner.type.fullname):
                winner = super_meta
                continue
            # metaclass conflict
            winner = None
            break

        return winner

    def explain_metaclass_conflict(self) -> str | None:
        # Compare to logic in calculate_metaclass_type
        declared = self.declared_metaclass
        if declared is not None and not declared.type.has_base("builtins.type"):
            return None
        if self._fullname == "builtins.type":
            return None

        winner = declared
        if declared is None:
            resolution_steps = []
        else:
            resolution_steps = [f'"{declared.type.fullname}" (metaclass of "{self.fullname}")']
        for super_class in self.mro[1:]:
            super_meta = super_class.declared_metaclass
            if super_meta is None or super_meta.type is None:
                continue
            if winner is None:
                winner = super_meta
                resolution_steps.append(
                    f'"{winner.type.fullname}" (metaclass of "{super_class.fullname}")'
                )
                continue
            if winner.type.has_base(super_meta.type.fullname):
                continue
            if super_meta.type.has_base(winner.type.fullname):
                winner = super_meta
                resolution_steps.append(
                    f'"{winner.type.fullname}" (metaclass of "{super_class.fullname}")'
                )
                continue
            # metaclass conflict
            conflict = f'"{super_meta.type.fullname}" (metaclass of "{super_class.fullname}")'
            return f"{' > '.join(resolution_steps)} conflicts with {conflict}"

        return None

    def is_metaclass(self, *, precise: bool = False) -> bool:
        return (
            self.has_base("builtins.type")
            or self.fullname == "abc.ABCMeta"
            or (self.fallback_to_any and not precise)
        )

    def has_base(self, fullname: str) -> bool:
        """Return True if type has a base type with the specified name.

        This can be either via extension or via implementation.
        """
        for cls in self.mro:
            if cls.fullname == fullname:
                return True
        return False

    def direct_base_classes(self) -> list[TypeInfo]:
        """Return a direct base classes.

        Omit base classes of other base classes.
        """
        return [base.type for base in self.bases]

    def update_tuple_type(self, typ: mypy.types.TupleType) -> None:
        """Update tuple_type and special_alias as needed."""
        self.tuple_type = typ
        alias = TypeAlias.from_tuple_type(self)
        if not self.special_alias:
            self.special_alias = alias
        else:
            self.special_alias.target = alias.target

    def update_typeddict_type(self, typ: mypy.types.TypedDictType) -> None:
        """Update typeddict_type and special_alias as needed."""
        self.typeddict_type = typ
        alias = TypeAlias.from_typeddict_type(self)
        if not self.special_alias:
            self.special_alias = alias
        else:
            self.special_alias.target = alias.target

    def __str__(self) -> str:
        """Return a string representation of the type.

        This includes the most important information about the type.
        """
        options = Options()
        return self.dump(
            str_conv=mypy.strconv.StrConv(options=options),
            type_str_conv=mypy.types.TypeStrVisitor(options=options),
        )

    def dump(
        self, str_conv: mypy.strconv.StrConv, type_str_conv: mypy.types.TypeStrVisitor
    ) -> str:
        """Return a string dump of the contents of the TypeInfo."""

        base: str = ""

        def type_str(typ: mypy.types.Type) -> str:
            return typ.accept(type_str_conv)

        head = "TypeInfo" + str_conv.format_id(self)
        if self.bases:
            base = f"Bases({', '.join(type_str(base) for base in self.bases)})"
        mro = "Mro({})".format(
            ", ".join(item.fullname + str_conv.format_id(item) for item in self.mro)
        )
        names = []
        for name in sorted(self.names):
            description = name + str_conv.format_id(self.names[name].node)
            node = self.names[name].node
            if isinstance(node, Var) and node.type:
                description += f" ({type_str(node.type)})"
            names.append(description)
        items = [f"Name({self.fullname})", base, mro, ("Names", names)]
        if self.declared_metaclass:
            items.append(f"DeclaredMetaclass({type_str(self.declared_metaclass)})")
        if self.metaclass_type:
            items.append(f"MetaclassType({type_str(self.metaclass_type)})")
        return mypy.strconv.dump_tagged(items, head, str_conv=str_conv)

    def serialize(self) -> JsonDict:
        # NOTE: This is where all ClassDefs originate, so there shouldn't be duplicates.
        data = {
            ".class": "TypeInfo",
            "module_name": self.module_name,
            "fullname": self.fullname,
            "names": self.names.serialize(self.fullname),
            "defn": self.defn.serialize(),
            "abstract_attributes": self.abstract_attributes,
            "type_vars": self.type_vars,
            "has_param_spec_type": self.has_param_spec_type,
            "bases": [b.serialize() for b in self.bases],
            "mro": [c.fullname for c in self.mro],
            "_promote": [p.serialize() for p in self._promote],
            "alt_promote": None if self.alt_promote is None else self.alt_promote.serialize(),
            "declared_metaclass": (
                None if self.declared_metaclass is None else self.declared_metaclass.serialize()
            ),
            "metaclass_type": (
                None if self.metaclass_type is None else self.metaclass_type.serialize()
            ),
            "tuple_type": None if self.tuple_type is None else self.tuple_type.serialize(),
            "typeddict_type": (
                None if self.typeddict_type is None else self.typeddict_type.serialize()
            ),
            "flags": get_flags(self, TypeInfo.FLAGS),
            "metadata": self.metadata,
            "slots": sorted(self.slots) if self.slots is not None else None,
            "deletable_attributes": self.deletable_attributes,
            "self_type": self.self_type.serialize() if self.self_type is not None else None,
            "dataclass_transform_spec": (
                self.dataclass_transform_spec.serialize()
                if self.dataclass_transform_spec is not None
                else None
            ),
            "deprecated": self.deprecated,
        }
        return data

    @classmethod
    def deserialize(cls, data: JsonDict) -> TypeInfo:
        names = SymbolTable.deserialize(data["names"])
        defn = ClassDef.deserialize(data["defn"])
        module_name = data["module_name"]
        ti = TypeInfo(names, defn, module_name)
        ti._fullname = data["fullname"]
        # TODO: Is there a reason to reconstruct ti.subtypes?
        ti.abstract_attributes = [(attr[0], attr[1]) for attr in data["abstract_attributes"]]
        ti.type_vars = data["type_vars"]
        ti.has_param_spec_type = data["has_param_spec_type"]
        ti.bases = [mypy.types.Instance.deserialize(b) for b in data["bases"]]
        _promote = []
        for p in data["_promote"]:
            t = mypy.types.deserialize_type(p)
            assert isinstance(t, mypy.types.ProperType)
            _promote.append(t)
        ti._promote = _promote
        ti.alt_promote = (
            None
            if data["alt_promote"] is None
            else mypy.types.Instance.deserialize(data["alt_promote"])
        )
        ti.declared_metaclass = (
            None
            if data["declared_metaclass"] is None
            else mypy.types.Instance.deserialize(data["declared_metaclass"])
        )
        ti.metaclass_type = (
            None
            if data["metaclass_type"] is None
            else mypy.types.Instance.deserialize(data["metaclass_type"])
        )
        # NOTE: ti.mro will be set in the fixup phase based on these
        # names.  The reason we need to store the mro instead of just
        # recomputing it from base classes has to do with a subtle
        # point about fine-grained incremental: the cache files might
        # not be loaded until after a class in the mro has changed its
        # bases, which causes the mro to change. If we recomputed our
        # mro, we would compute the *new* mro, which leaves us with no
        # way to detect that the mro has changed! Thus we need to make
        # sure to load the original mro so that once the class is
        # rechecked, it can tell that the mro has changed.
        ti._mro_refs = data["mro"]
        ti.tuple_type = (
            None
            if data["tuple_type"] is None
            else mypy.types.TupleType.deserialize(data["tuple_type"])
        )
        ti.typeddict_type = (
            None
            if data["typeddict_type"] is None
            else mypy.types.TypedDictType.deserialize(data["typeddict_type"])
        )
        ti.metadata = data["metadata"]
        ti.slots = set(data["slots"]) if data["slots"] is not None else None
        ti.deletable_attributes = data["deletable_attributes"]
        set_flags(ti, data["flags"])
        st = data["self_type"]
        ti.self_type = mypy.types.TypeVarType.deserialize(st) if st is not None else None
        if data.get("dataclass_transform_spec") is not None:
            ti.dataclass_transform_spec = DataclassTransformSpec.deserialize(
                data["dataclass_transform_spec"]
            )
        ti.deprecated = data.get("deprecated")
        return ti


class FakeInfo(TypeInfo):
    __slots__ = ("msg",)

    # types.py defines a single instance of this class, called types.NOT_READY.
    # This instance is used as a temporary placeholder in the process of de-serialization
    # of 'Instance' types. The de-serialization happens in two steps: In the first step,
    # Instance.type is set to NOT_READY. In the second step (in fixup.py) it is replaced by
    # an actual TypeInfo. If you see the assertion error below, then most probably something
    # went wrong during the second step and an 'Instance' that raised this error was not fixed.
    # Note:
    # 'None' is not used as a dummy value for two reasons:
    # 1. This will require around 80-100 asserts to make 'mypy --strict-optional mypy'
    #    pass cleanly.
    # 2. If NOT_READY value is accidentally used somewhere, it will be obvious where the value
    #    is from, whereas a 'None' value could come from anywhere.
    #
    # Additionally, this serves as a more general-purpose placeholder
    # for missing TypeInfos in a number of places where the excuses
    # for not being Optional are a little weaker.
    #
    # TypeInfo defines a __bool__ method that returns False for FakeInfo
    # so that it can be conveniently tested against in the same way that it
    # would be if things were properly optional.
    def __init__(self, msg: str) -> None:
        self.msg = msg

    def __getattribute__(self, attr: str) -> type:
        # Handle __class__ so that isinstance still works...
        if attr == "__class__":
            return object.__getattribute__(self, attr)  # type: ignore[no-any-return]
        raise AssertionError(object.__getattribute__(self, "msg"))


VAR_NO_INFO: Final[TypeInfo] = FakeInfo("Var is lacking info")
CLASSDEF_NO_INFO: Final[TypeInfo] = FakeInfo("ClassDef is lacking info")
FUNC_NO_INFO: Final[TypeInfo] = FakeInfo("FuncBase for non-methods lack info")
MISSING_FALLBACK: Final = FakeInfo("fallback can't be filled out until semanal")


class TypeAlias(SymbolNode):
    """
    A symbol node representing a type alias.

    Type alias is a static concept, in contrast to variables with types
    like Type[...]. Namely:
        * type aliases
            - can be used in type context (annotations)
            - cannot be re-assigned
        * variables with type Type[...]
            - cannot be used in type context
            - but can be re-assigned

    An alias can be defined only by an assignment to a name (not any other lvalues).

    Such assignment defines an alias by default. To define a variable,
    an explicit Type[...] annotation is required. As an exception,
    at non-global scope non-subscripted rvalue creates a variable even without
    an annotation. This exception exists to accommodate the common use case of
    class-valued attributes. See SemanticAnalyzerPass2.check_and_set_up_type_alias
    for details.

    Aliases can be generic. We use bound type variables for generic aliases, similar
    to classes. Essentially, type aliases work as macros that expand textually.
    The definition and expansion rules are following:

        1. An alias targeting a generic class without explicit variables act as
        the given class (this doesn't apply to TypedDict, Tuple and Callable, which
        are not proper classes but special type constructors):

            A = List
            AA = List[Any]

            x: A  # same as List[Any]
            x: A[int]  # same as List[int]

            x: AA  # same as List[Any]
            x: AA[int]  # Error!

            C = Callable  # Same as Callable[..., Any]
            T = Tuple  # Same as Tuple[Any, ...]

        2. An alias using explicit type variables in its rvalue expects
        replacements (type arguments) for these variables. If missing, they
        are treated as Any, like for other generics:

            B = List[Tuple[T, T]]

            x: B  # same as List[Tuple[Any, Any]]
            x: B[int]  # same as List[Tuple[int, int]]

            def f(x: B[T]) -> T: ...  # without T, Any would be used here

        3. An alias can be defined using another aliases. In the definition
        rvalue the Any substitution doesn't happen for top level unsubscripted
        generic classes:

            A = List
            B = A  # here A is expanded to List, _not_ List[Any],
                   # to match the Python runtime behaviour
            x: B[int]  # same as List[int]
            C = List[A]  # this expands to List[List[Any]]

            AA = List[T]
            D = AA  # here AA expands to List[Any]
            x: D[int]  # Error!

    Note: the fact that we support aliases like `A = List` means that the target
    type will be initially an instance type with wrong number of type arguments.
    Such instances are all fixed either during or after main semantic analysis passes.
    We therefore store the difference between `List` and `List[Any]` rvalues (targets)
    using the `no_args` flag.

    Meaning of other fields:

    target: The target type. For generic aliases contains bound type variables
        as nested types (currently TypeVar and ParamSpec are supported).
    _fullname: Qualified name of this type alias. This is used in particular
        to track fine grained dependencies from aliases.
    alias_tvars: Type variables used to define this alias.
    normalized: Used to distinguish between `A = List`, and `A = list`. Both
        are internally stored using `builtins.list` (because `typing.List` is
        itself an alias), while the second cannot be subscripted because of
        Python runtime limitation.
    line and column: Line and column on the original alias definition.
    eager: If True, immediately expand alias when referred to (useful for aliases
        within functions that can't be looked up from the symbol table)
    """

    __slots__ = (
        "target",
        "_fullname",
        "alias_tvars",
        "no_args",
        "normalized",
        "_is_recursive",
        "eager",
        "tvar_tuple_index",
        "python_3_12_type_alias",
    )

    __match_args__ = ("name", "target", "alias_tvars", "no_args")

    def __init__(
        self,
        target: mypy.types.Type,
        fullname: str,
        line: int,
        column: int,
        *,
        alias_tvars: list[mypy.types.TypeVarLikeType] | None = None,
        no_args: bool = False,
        normalized: bool = False,
        eager: bool = False,
        python_3_12_type_alias: bool = False,
    ) -> None:
        self._fullname = fullname
        self.target = target
        if alias_tvars is None:
            alias_tvars = []
        self.alias_tvars = alias_tvars
        self.no_args = no_args
        self.normalized = normalized
        # This attribute is manipulated by TypeAliasType. If non-None,
        # it is the cached value.
        self._is_recursive: bool | None = None
        self.eager = eager
        self.python_3_12_type_alias = python_3_12_type_alias
        self.tvar_tuple_index = None
        for i, t in enumerate(alias_tvars):
            if isinstance(t, mypy.types.TypeVarTupleType):
                self.tvar_tuple_index = i
        super().__init__(line, column)

    @classmethod
    def from_tuple_type(cls, info: TypeInfo) -> TypeAlias:
        """Generate an alias to the tuple type described by a given TypeInfo.

        NOTE: this doesn't set type alias type variables (for generic tuple types),
        they must be set by the caller (when fully analyzed).
        """
        assert info.tuple_type
        # TODO: is it possible to refactor this to set the correct type vars here?
        return TypeAlias(
            info.tuple_type.copy_modified(
                # Create an Instance similar to fill_typevars().
                fallback=mypy.types.Instance(
                    info, mypy.types.type_vars_as_args(info.defn.type_vars)
                )
            ),
            info.fullname,
            info.line,
            info.column,
        )

    @classmethod
    def from_typeddict_type(cls, info: TypeInfo) -> TypeAlias:
        """Generate an alias to the TypedDict type described by a given TypeInfo.

        NOTE: this doesn't set type alias type variables (for generic TypedDicts),
        they must be set by the caller (when fully analyzed).
        """
        assert info.typeddict_type
        # TODO: is it possible to refactor this to set the correct type vars here?
        return TypeAlias(
            info.typeddict_type.copy_modified(
                # Create an Instance similar to fill_typevars().
                fallback=mypy.types.Instance(
                    info, mypy.types.type_vars_as_args(info.defn.type_vars)
                )
            ),
            info.fullname,
            info.line,
            info.column,
        )

    @property
    def name(self) -> str:
        return self._fullname.split(".")[-1]

    @property
    def fullname(self) -> str:
        return self._fullname

    @property
    def has_param_spec_type(self) -> bool:
        return any(isinstance(v, mypy.types.ParamSpecType) for v in self.alias_tvars)

    def serialize(self) -> JsonDict:
        data: JsonDict = {
            ".class": "TypeAlias",
            "fullname": self._fullname,
            "target": self.target.serialize(),
            "alias_tvars": [v.serialize() for v in self.alias_tvars],
            "no_args": self.no_args,
            "normalized": self.normalized,
            "line": self.line,
            "column": self.column,
            "python_3_12_type_alias": self.python_3_12_type_alias,
        }
        return data

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_type_alias(self)

    @classmethod
    def deserialize(cls, data: JsonDict) -> TypeAlias:
        assert data[".class"] == "TypeAlias"
        fullname = data["fullname"]
        alias_tvars = [mypy.types.deserialize_type(v) for v in data["alias_tvars"]]
        assert all(isinstance(t, mypy.types.TypeVarLikeType) for t in alias_tvars)
        target = mypy.types.deserialize_type(data["target"])
        no_args = data["no_args"]
        normalized = data["normalized"]
        line = data["line"]
        column = data["column"]
        python_3_12_type_alias = data["python_3_12_type_alias"]
        return cls(
            target,
            fullname,
            line,
            column,
            alias_tvars=cast(list[mypy.types.TypeVarLikeType], alias_tvars),
            no_args=no_args,
            normalized=normalized,
            python_3_12_type_alias=python_3_12_type_alias,
        )


class PlaceholderNode(SymbolNode):
    """Temporary symbol node that will later become a real SymbolNode.

    These are only present during semantic analysis when using the new
    semantic analyzer. These are created if some essential dependencies
    of a definition are not yet complete.

    A typical use is for names imported from a module which is still
    incomplete (within an import cycle):

      from m import f  # Initially may create PlaceholderNode

    This is particularly important if the imported shadows a name from
    an enclosing scope or builtins:

      from m import int  # Placeholder avoids mixups with builtins.int

    Another case where this is useful is when there is another definition
    or assignment:

      from m import f
      def f() -> None: ...

    In the above example, the presence of PlaceholderNode allows us to
    handle the second definition as a redefinition.

    They are also used to create PlaceholderType instances for types
    that refer to incomplete types. Example:

      class C(Sequence[C]): ...

    We create a PlaceholderNode (with becomes_typeinfo=True) for C so
    that the type C in Sequence[C] can be bound.

    Attributes:

      fullname: Full name of the PlaceholderNode.
      node: AST node that contains the definition that caused this to
          be created. This is useful for tracking order of incomplete definitions
          and for debugging.
      becomes_typeinfo: If True, this refers something that could later
          become a TypeInfo. It can't be used with type variables, in
          particular, as this would cause issues with class type variable
          detection.

    The long-term purpose of placeholder nodes/types is to evolve into
    something that can support general recursive types.
    """

    __slots__ = ("_fullname", "node", "becomes_typeinfo")

    def __init__(
        self, fullname: str, node: Node, line: int, *, becomes_typeinfo: bool = False
    ) -> None:
        self._fullname = fullname
        self.node = node
        self.becomes_typeinfo = becomes_typeinfo
        self.line = line

    @property
    def name(self) -> str:
        return self._fullname.split(".")[-1]

    @property
    def fullname(self) -> str:
        return self._fullname

    def serialize(self) -> JsonDict:
        assert False, "PlaceholderNode can't be serialized"

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_placeholder_node(self)


class SymbolTableNode:
    """Description of a name binding in a symbol table.

    These are only used as values in module (global), function (local)
    and class symbol tables (see SymbolTable). The name that is bound is
    the key in SymbolTable.

    Symbol tables don't contain direct references to AST nodes primarily
    because there can be multiple symbol table references to a single
    AST node (due to imports and aliases), and different references can
    behave differently. This class describes the unique properties of
    each reference.

    The most fundamental attribute is 'node', which is the AST node that
    the name refers to.

    The kind is usually one of LDEF, GDEF or MDEF, depending on the scope
    of the definition. These three kinds can usually be used
    interchangeably and the difference between local, global and class
    scopes is mostly descriptive, with no semantic significance.
    However, some tools that consume mypy ASTs may care about these so
    they should be correct.

    Attributes:
        node: AST node of definition. Among others, this can be one of
            FuncDef, Var, TypeInfo, TypeVarExpr or MypyFile -- or None
            for cross_ref that hasn't been fixed up yet.
        kind: Kind of node. Possible values:
               - LDEF: local definition
               - GDEF: global (module-level) definition
               - MDEF: class member definition
               - UNBOUND_IMPORTED: temporary kind for imported names (we
                 don't know the final kind yet)
        module_public: If False, this name won't be imported via
            'from <module> import *'. This has no effect on names within
            classes.
        module_hidden: If True, the name will be never exported (needed for
            stub files)
        cross_ref: For deserialized MypyFile nodes, the referenced module
            name; for other nodes, optionally the name of the referenced object.
        implicit: Was this defined by assignment to self attribute?
        plugin_generated: Was this symbol generated by a plugin?
            (And therefore needs to be removed in aststrip.)
        no_serialize: Do not serialize this node if True. This is used to prevent
            keys in the cache that refer to modules on which this file does not
            depend. Currently this can happen if there is a module not in build
            used e.g. like this:
                import a.b.c # type: ignore
            This will add a submodule symbol to parent module `a` symbol table,
            but `a.b` is _not_ added as its dependency. Therefore, we should
            not serialize these symbols as they may not be found during fixup
            phase, instead they will be re-added during subsequent patch parents
            phase.
            TODO: Refactor build.py to make dependency tracking more transparent
            and/or refactor look-up functions to not require parent patching.

    NOTE: No other attributes should be added to this class unless they
    are shared by all node kinds.
    """

    __slots__ = (
        "kind",
        "node",
        "module_public",
        "module_hidden",
        "cross_ref",
        "implicit",
        "plugin_generated",
        "no_serialize",
    )

    def __init__(
        self,
        kind: int,
        node: SymbolNode | None,
        module_public: bool = True,
        implicit: bool = False,
        module_hidden: bool = False,
        *,
        plugin_generated: bool = False,
        no_serialize: bool = False,
    ) -> None:
        self.kind = kind
        self.node = node
        self.module_public = module_public
        self.implicit = implicit
        self.module_hidden = module_hidden
        self.cross_ref: str | None = None
        self.plugin_generated = plugin_generated
        self.no_serialize = no_serialize

    @property
    def fullname(self) -> str | None:
        if self.node is not None:
            return self.node.fullname
        else:
            return None

    @property
    def type(self) -> mypy.types.Type | None:
        node = self.node
        if isinstance(node, (Var, SYMBOL_FUNCBASE_TYPES)) and node.type is not None:
            return node.type
        elif isinstance(node, Decorator):
            return node.var.type
        else:
            return None

    def copy(self) -> SymbolTableNode:
        new = SymbolTableNode(
            self.kind, self.node, self.module_public, self.implicit, self.module_hidden
        )
        new.cross_ref = self.cross_ref
        return new

    def __str__(self) -> str:
        s = f"{node_kinds[self.kind]}/{short_type(self.node)}"
        if isinstance(self.node, SymbolNode):
            s += f" ({self.node.fullname})"
        # Include declared type of variables and functions.
        if self.type is not None:
            s += f" : {self.type}"
        if self.cross_ref:
            s += f" cross_ref:{self.cross_ref}"
        return s

    def serialize(self, prefix: str, name: str) -> JsonDict:
        """Serialize a SymbolTableNode.

        Args:
          prefix: full name of the containing module or class; or None
          name: name of this object relative to the containing object
        """
        data: JsonDict = {".class": "SymbolTableNode", "kind": node_kinds[self.kind]}
        if self.module_hidden:
            data["module_hidden"] = True
        if not self.module_public:
            data["module_public"] = False
        if self.implicit:
            data["implicit"] = True
        if self.plugin_generated:
            data["plugin_generated"] = True
        if isinstance(self.node, MypyFile):
            data["cross_ref"] = self.node.fullname
        else:
            assert self.node is not None, f"{prefix}:{name}"
            if prefix is not None:
                fullname = self.node.fullname
                if (
                    "." in fullname
                    and fullname != prefix + "." + name
                    and not (isinstance(self.node, Var) and self.node.from_module_getattr)
                ):
                    assert not isinstance(
                        self.node, PlaceholderNode
                    ), f"Definition of {fullname} is unexpectedly incomplete"
                    data["cross_ref"] = fullname
                    return data
            data["node"] = self.node.serialize()
        return data

    @classmethod
    def deserialize(cls, data: JsonDict) -> SymbolTableNode:
        assert data[".class"] == "SymbolTableNode"
        kind = inverse_node_kinds[data["kind"]]
        if "cross_ref" in data:
            # This will be fixed up later.
            stnode = SymbolTableNode(kind, None)
            stnode.cross_ref = data["cross_ref"]
        else:
            assert "node" in data, data
            node = SymbolNode.deserialize(data["node"])
            stnode = SymbolTableNode(kind, node)
        if "module_hidden" in data:
            stnode.module_hidden = data["module_hidden"]
        if "module_public" in data:
            stnode.module_public = data["module_public"]
        if "implicit" in data:
            stnode.implicit = data["implicit"]
        if "plugin_generated" in data:
            stnode.plugin_generated = data["plugin_generated"]
        return stnode


class SymbolTable(dict[str, SymbolTableNode]):
    """Static representation of a namespace dictionary.

    This is used for module, class and function namespaces.
    """

    __slots__ = ()

    def __str__(self) -> str:
        a: list[str] = []
        for key, value in self.items():
            # Filter out the implicit import of builtins.
            if isinstance(value, SymbolTableNode):
                if (
                    value.fullname != "builtins"
                    and (value.fullname or "").split(".")[-1] not in implicit_module_attrs
                ):
                    a.append("  " + str(key) + " : " + str(value))
            else:
                # Used in debugging:
                a.append("  <invalid item>")  # type: ignore[unreachable]
        a = sorted(a)
        a.insert(0, "SymbolTable(")
        a[-1] += ")"
        return "\n".join(a)

    def copy(self) -> SymbolTable:
        return SymbolTable([(key, node.copy()) for key, node in self.items()])

    def serialize(self, fullname: str) -> JsonDict:
        data: JsonDict = {".class": "SymbolTable"}
        for key, value in self.items():
            # Skip __builtins__: it's a reference to the builtins
            # module that gets added to every module by
            # SemanticAnalyzerPass2.visit_file(), but it shouldn't be
            # accessed by users of the module.
            if key == "__builtins__" or value.no_serialize:
                continue
            data[key] = value.serialize(fullname, key)
        return data

    @classmethod
    def deserialize(cls, data: JsonDict) -> SymbolTable:
        assert data[".class"] == "SymbolTable"
        st = SymbolTable()
        for key, value in data.items():
            if key != ".class":
                st[key] = SymbolTableNode.deserialize(value)
        return st


class DataclassTransformSpec:
    """Specifies how a dataclass-like transform should be applied. The fields here are based on the
    parameters accepted by `typing.dataclass_transform`."""

    __slots__ = (
        "eq_default",
        "order_default",
        "kw_only_default",
        "frozen_default",
        "field_specifiers",
    )

    def __init__(
        self,
        *,
        eq_default: bool | None = None,
        order_default: bool | None = None,
        kw_only_default: bool | None = None,
        field_specifiers: tuple[str, ...] | None = None,
        # Specified outside of PEP 681:
        # frozen_default was added to CPythonin https://github.com/python/cpython/pull/99958 citing
        # positive discussion in typing-sig
        frozen_default: bool | None = None,
    ) -> None:
        self.eq_default = eq_default if eq_default is not None else True
        self.order_default = order_default if order_default is not None else False
        self.kw_only_default = kw_only_default if kw_only_default is not None else False
        self.frozen_default = frozen_default if frozen_default is not None else False
        self.field_specifiers = field_specifiers if field_specifiers is not None else ()

    def serialize(self) -> JsonDict:
        return {
            "eq_default": self.eq_default,
            "order_default": self.order_default,
            "kw_only_default": self.kw_only_default,
            "frozen_default": self.frozen_default,
            "field_specifiers": list(self.field_specifiers),
        }

    @classmethod
    def deserialize(cls, data: JsonDict) -> DataclassTransformSpec:
        return DataclassTransformSpec(
            eq_default=data.get("eq_default"),
            order_default=data.get("order_default"),
            kw_only_default=data.get("kw_only_default"),
            frozen_default=data.get("frozen_default"),
            field_specifiers=tuple(data.get("field_specifiers", [])),
        )


def get_flags(node: Node, names: list[str]) -> list[str]:
    return [name for name in names if getattr(node, name)]


def set_flags(node: Node, flags: list[str]) -> None:
    for name in flags:
        setattr(node, name, True)


def get_member_expr_fullname(expr: MemberExpr) -> str | None:
    """Return the qualified name representation of a member expression.

    Return a string of form foo.bar, foo.bar.baz, or similar, or None if the
    argument cannot be represented in this form.
    """
    initial: str | None = None
    if isinstance(expr.expr, NameExpr):
        initial = expr.expr.name
    elif isinstance(expr.expr, MemberExpr):
        initial = get_member_expr_fullname(expr.expr)
    if initial is None:
        return None
    return f"{initial}.{expr.name}"


deserialize_map: Final = {
    key: obj.deserialize
    for key, obj in globals().items()
    if type(obj) is not FakeInfo
    and isinstance(obj, type)
    and issubclass(obj, SymbolNode)
    and obj is not SymbolNode
}


def check_arg_kinds(
    arg_kinds: list[ArgKind], nodes: list[T], fail: Callable[[str, T], None]
) -> None:
    is_var_arg = False
    is_kw_arg = False
    seen_named = False
    seen_opt = False
    for kind, node in zip(arg_kinds, nodes):
        if kind == ARG_POS:
            if is_var_arg or is_kw_arg or seen_named or seen_opt:
                fail(
                    "Required positional args may not appear after default, named or var args",
                    node,
                )
                break
        elif kind == ARG_OPT:
            if is_var_arg or is_kw_arg or seen_named:
                fail("Positional default args may not appear after named or var args", node)
                break
            seen_opt = True
        elif kind == ARG_STAR:
            if is_var_arg or is_kw_arg or seen_named:
                fail("Var args may not appear after named or var args", node)
                break
            is_var_arg = True
        elif kind == ARG_NAMED or kind == ARG_NAMED_OPT:
            seen_named = True
            if is_kw_arg:
                fail("A **kwargs argument must be the last argument", node)
                break
        elif kind == ARG_STAR2:
            if is_kw_arg:
                fail("You may only have one **kwargs argument", node)
                break
            is_kw_arg = True


def check_arg_names(
    names: Sequence[str | None],
    nodes: list[T],
    fail: Callable[[str, T], None],
    description: str = "function definition",
) -> None:
    seen_names: set[str | None] = set()
    for name, node in zip(names, nodes):
        if name is not None and name in seen_names:
            fail(f'Duplicate argument "{name}" in {description}', node)
            break
        seen_names.add(name)


def is_class_var(expr: NameExpr) -> bool:
    """Return whether the expression is ClassVar[...]"""
    if isinstance(expr.node, Var):
        return expr.node.is_classvar
    return False


def is_final_node(node: SymbolNode | None) -> bool:
    """Check whether `node` corresponds to a final attribute."""
    return isinstance(node, (Var, FuncDef, OverloadedFuncDef, Decorator)) and node.is_final


def local_definitions(
    names: SymbolTable, name_prefix: str, info: TypeInfo | None = None
) -> Iterator[Definition]:
    """Iterate over local definitions (not imported) in a symbol table.

    Recursively iterate over class members and nested classes.
    """
    # TODO: What should the name be? Or maybe remove it?
    for name, symnode in names.items():
        shortname = name
        if "-redef" in name:
            # Restore original name from mangled name of multiply defined function
            shortname = name.split("-redef")[0]
        fullname = name_prefix + "." + shortname
        node = symnode.node
        if node and node.fullname == fullname:
            yield fullname, symnode, info
            if isinstance(node, TypeInfo):
                yield from local_definitions(node.names, fullname, node)
