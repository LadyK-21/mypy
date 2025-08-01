"""Expression type checker. This file is conceptually part of TypeChecker."""

from __future__ import annotations

import enum
import itertools
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from typing import Callable, ClassVar, Final, Optional, cast, overload
from typing_extensions import TypeAlias as _TypeAlias, assert_never

import mypy.checker
import mypy.errorcodes as codes
from mypy import applytype, erasetype, join, message_registry, nodes, operators, types
from mypy.argmap import ArgTypeExpander, map_actuals_to_formals, map_formals_to_actuals
from mypy.checker_shared import ExpressionCheckerSharedApi
from mypy.checkmember import analyze_member_access, has_operator
from mypy.checkstrformat import StringFormatterChecker
from mypy.erasetype import erase_type, remove_instance_last_known_values, replace_meta_vars
from mypy.errors import ErrorInfo, ErrorWatcher, report_internal_error
from mypy.expandtype import (
    expand_type,
    expand_type_by_instance,
    freshen_all_functions_type_vars,
    freshen_function_type_vars,
)
from mypy.infer import ArgumentInferContext, infer_function_type_arguments, infer_type_arguments
from mypy.literals import literal
from mypy.maptype import map_instance_to_supertype
from mypy.meet import is_overlapping_types, narrow_declared_type
from mypy.message_registry import ErrorMessage
from mypy.messages import MessageBuilder, format_type
from mypy.nodes import (
    ARG_NAMED,
    ARG_POS,
    ARG_STAR,
    ARG_STAR2,
    IMPLICITLY_ABSTRACT,
    LAMBDA_NAME,
    LITERAL_TYPE,
    REVEAL_LOCALS,
    REVEAL_TYPE,
    ArgKind,
    AssertTypeExpr,
    AssignmentExpr,
    AwaitExpr,
    BytesExpr,
    CallExpr,
    CastExpr,
    ComparisonExpr,
    ComplexExpr,
    ConditionalExpr,
    Context,
    Decorator,
    DictExpr,
    DictionaryComprehension,
    EllipsisExpr,
    EnumCallExpr,
    Expression,
    FloatExpr,
    FuncDef,
    GeneratorExpr,
    IndexExpr,
    IntExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    MemberExpr,
    MypyFile,
    NamedTupleExpr,
    NameExpr,
    NewTypeExpr,
    OpExpr,
    OverloadedFuncDef,
    ParamSpecExpr,
    PlaceholderNode,
    PromoteExpr,
    RefExpr,
    RevealExpr,
    SetComprehension,
    SetExpr,
    SliceExpr,
    StarExpr,
    StrExpr,
    SuperExpr,
    SymbolNode,
    TempNode,
    TupleExpr,
    TypeAlias,
    TypeAliasExpr,
    TypeApplication,
    TypedDictExpr,
    TypeInfo,
    TypeVarExpr,
    TypeVarLikeExpr,
    TypeVarTupleExpr,
    UnaryExpr,
    Var,
    YieldExpr,
    YieldFromExpr,
)
from mypy.options import PRECISE_TUPLE_TYPES
from mypy.plugin import (
    FunctionContext,
    FunctionSigContext,
    MethodContext,
    MethodSigContext,
    Plugin,
)
from mypy.semanal_enum import ENUM_BASES
from mypy.state import state
from mypy.subtypes import (
    find_member,
    is_equivalent,
    is_same_type,
    is_subtype,
    non_method_protocol_members,
)
from mypy.traverser import has_await_expression
from mypy.typeanal import (
    check_for_explicit_any,
    fix_instance,
    has_any_from_unimported_type,
    instantiate_type_alias,
    make_optional_type,
    set_any_tvars,
    validate_instance,
)
from mypy.typeops import (
    bind_self,
    callable_type,
    custom_special_method,
    erase_to_union_or_bound,
    false_only,
    fixup_partial_type,
    freeze_all_type_vars,
    function_type,
    get_all_type_vars,
    get_type_vars,
    is_literal_type_like,
    make_simplified_union,
    simple_literal_type,
    true_only,
    try_expanding_sum_type_to_union,
    try_getting_str_literals,
    tuple_fallback,
    type_object_type,
)
from mypy.types import (
    LITERAL_TYPE_NAMES,
    TUPLE_LIKE_INSTANCE_NAMES,
    AnyType,
    CallableType,
    DeletedType,
    ErasedType,
    ExtraAttrs,
    FunctionLike,
    Instance,
    LiteralType,
    LiteralValue,
    NoneType,
    Overloaded,
    Parameters,
    ParamSpecFlavor,
    ParamSpecType,
    PartialType,
    ProperType,
    TupleType,
    Type,
    TypeAliasType,
    TypedDictType,
    TypeOfAny,
    TypeType,
    TypeVarId,
    TypeVarLikeType,
    TypeVarTupleType,
    TypeVarType,
    UnboundType,
    UninhabitedType,
    UnionType,
    UnpackType,
    find_unpack_in_list,
    flatten_nested_tuples,
    flatten_nested_unions,
    get_proper_type,
    get_proper_types,
    has_recursive_types,
    has_type_vars,
    is_named_instance,
    split_with_prefix_and_suffix,
)
from mypy.types_utils import (
    is_generic_instance,
    is_overlapping_none,
    is_self_type_like,
    remove_optional,
)
from mypy.typestate import type_state
from mypy.typevars import fill_typevars
from mypy.util import split_module_names
from mypy.visitor import ExpressionVisitor

# Type of callback user for checking individual function arguments. See
# check_args() below for details.
ArgChecker: _TypeAlias = Callable[
    [Type, Type, ArgKind, Type, int, int, CallableType, Optional[Type], Context, Context], None
]

# Maximum nesting level for math union in overloads, setting this to large values
# may cause performance issues. The reason is that although union math algorithm we use
# nicely captures most corner cases, its worst case complexity is exponential,
# see https://github.com/python/mypy/pull/5255#discussion_r196896335 for discussion.
MAX_UNIONS: Final = 5


# Types considered safe for comparisons with --strict-equality due to known behaviour of __eq__.
# NOTE: All these types are subtypes of AbstractSet.
OVERLAPPING_TYPES_ALLOWLIST: Final = [
    "builtins.set",
    "builtins.frozenset",
    "typing.KeysView",
    "typing.ItemsView",
    "builtins._dict_keys",
    "builtins._dict_items",
    "_collections_abc.dict_keys",
    "_collections_abc.dict_items",
]
OVERLAPPING_BYTES_ALLOWLIST: Final = {
    "builtins.bytes",
    "builtins.bytearray",
    "builtins.memoryview",
}


class TooManyUnions(Exception):
    """Indicates that we need to stop splitting unions in an attempt
    to match an overload in order to save performance.
    """


def allow_fast_container_literal(t: Type) -> bool:
    if isinstance(t, TypeAliasType) and t.is_recursive:
        return False
    t = get_proper_type(t)
    return isinstance(t, Instance) or (
        isinstance(t, TupleType) and all(allow_fast_container_literal(it) for it in t.items)
    )


def extract_refexpr_names(expr: RefExpr) -> set[str]:
    """Recursively extracts all module references from a reference expression.

    Note that currently, the only two subclasses of RefExpr are NameExpr and
    MemberExpr."""
    output: set[str] = set()
    while isinstance(expr.node, MypyFile) or expr.fullname:
        if isinstance(expr.node, MypyFile) and expr.fullname:
            # If it's None, something's wrong (perhaps due to an
            # import cycle or a suppressed error).  For now we just
            # skip it.
            output.add(expr.fullname)

        if isinstance(expr, NameExpr):
            is_suppressed_import = isinstance(expr.node, Var) and expr.node.is_suppressed_import
            if isinstance(expr.node, TypeInfo):
                # Reference to a class or a nested class
                output.update(split_module_names(expr.node.module_name))
            elif "." in expr.fullname and not is_suppressed_import:
                # Everything else (that is not a silenced import within a class)
                output.add(expr.fullname.rsplit(".", 1)[0])
            break
        elif isinstance(expr, MemberExpr):
            if isinstance(expr.expr, RefExpr):
                expr = expr.expr
            else:
                break
        else:
            raise AssertionError(f"Unknown RefExpr subclass: {type(expr)}")
    return output


class Finished(Exception):
    """Raised if we can terminate overload argument check early (no match)."""


@enum.unique
class UseReverse(enum.Enum):
    """Used in `visit_op_expr` to enable or disable reverse method checks."""

    DEFAULT = 0
    ALWAYS = 1
    NEVER = 2


USE_REVERSE_DEFAULT: Final = UseReverse.DEFAULT
USE_REVERSE_ALWAYS: Final = UseReverse.ALWAYS
USE_REVERSE_NEVER: Final = UseReverse.NEVER


class ExpressionChecker(ExpressionVisitor[Type], ExpressionCheckerSharedApi):
    """Expression type checker.

    This class works closely together with checker.TypeChecker.
    """

    # Some services are provided by a TypeChecker instance.
    chk: mypy.checker.TypeChecker
    # This is shared with TypeChecker, but stored also here for convenience.
    msg: MessageBuilder
    # Type context for type inference
    type_context: list[Type | None]

    # cache resolved types in some cases
    resolved_type: dict[Expression, ProperType]

    strfrm_checker: StringFormatterChecker
    plugin: Plugin

    _arg_infer_context_cache: ArgumentInferContext | None

    def __init__(
        self,
        chk: mypy.checker.TypeChecker,
        msg: MessageBuilder,
        plugin: Plugin,
        per_line_checking_time_ns: dict[int, int],
    ) -> None:
        """Construct an expression type checker."""
        self.chk = chk
        self.msg = msg
        self.plugin = plugin
        self.per_line_checking_time_ns = per_line_checking_time_ns
        self.collect_line_checking_stats = chk.options.line_checking_stats is not None
        # Are we already visiting some expression? This is used to avoid double counting
        # time for nested expressions.
        self.in_expression = False
        self.type_context = [None]

        # Temporary overrides for expression types. This is currently
        # used by the union math in overloads.
        # TODO: refactor this to use a pattern similar to one in
        # multiassign_from_union, or maybe even combine the two?
        self.type_overrides: dict[Expression, Type] = {}
        self.strfrm_checker = StringFormatterChecker(self.chk, self.msg)

        self.resolved_type = {}

        # Callee in a call expression is in some sense both runtime context and
        # type context, because we support things like C[int](...). Store information
        # on whether current expression is a callee, to give better error messages
        # related to type context.
        self.is_callee = False
        type_state.infer_polymorphic = not self.chk.options.old_type_inference

        self._arg_infer_context_cache = None
        self.expr_cache: dict[
            tuple[Expression, Type | None],
            tuple[int, Type, list[ErrorInfo], dict[Expression, Type]],
        ] = {}
        self.in_lambda_expr = False

    def reset(self) -> None:
        self.resolved_type = {}
        self.expr_cache.clear()

    def visit_name_expr(self, e: NameExpr) -> Type:
        """Type check a name expression.

        It can be of any kind: local, member or global.
        """
        self.chk.module_refs.update(extract_refexpr_names(e))
        result = self.analyze_ref_expr(e)
        narrowed = self.narrow_type_from_binder(e, result)
        self.chk.check_deprecated(e.node, e)
        return narrowed

    def analyze_ref_expr(self, e: RefExpr, lvalue: bool = False) -> Type:
        result: Type | None = None
        node = e.node

        if isinstance(e, NameExpr) and e.is_special_form:
            # A special form definition, nothing to check here.
            return AnyType(TypeOfAny.special_form)

        if isinstance(node, Var):
            # Variable reference.
            result = self.analyze_var_ref(node, e)
            if isinstance(result, PartialType):
                result = self.chk.handle_partial_var_type(result, lvalue, node, e)
        elif isinstance(node, Decorator):
            result = self.analyze_var_ref(node.var, e)
        elif isinstance(node, OverloadedFuncDef):
            if node.type is None:
                if self.chk.in_checked_function() and node.items:
                    self.chk.handle_cannot_determine_type(node.name, e)
                result = AnyType(TypeOfAny.from_error)
            else:
                result = node.type
        elif isinstance(node, (FuncDef, TypeInfo, TypeAlias, MypyFile, TypeVarLikeExpr)):
            result = self.analyze_static_reference(node, e, e.is_alias_rvalue or lvalue)
        else:
            if isinstance(node, PlaceholderNode):
                assert False, f"PlaceholderNode {node.fullname!r} leaked to checker"
            # Unknown reference; use any type implicitly to avoid
            # generating extra type errors.
            result = AnyType(TypeOfAny.from_error)
        if isinstance(node, TypeInfo):
            if isinstance(result, CallableType) and isinstance(  # type: ignore[misc]
                result.ret_type, Instance
            ):
                # We need to set correct line and column
                # TODO: always do this in type_object_type by passing the original context
                result.ret_type.line = e.line
                result.ret_type.column = e.column
            if is_type_type_context(self.type_context[-1]):
                # This is the type in a type[] expression, so substitute type
                # variables with Any.
                result = erasetype.erase_typevars(result)
        assert result is not None
        return result

    def analyze_static_reference(
        self,
        node: SymbolNode,
        ctx: Context,
        is_lvalue: bool,
        *,
        include_modules: bool = True,
        suppress_errors: bool = False,
    ) -> Type:
        """
        This is the version of analyze_ref_expr() that doesn't do any deferrals.

        This function can be used by member access to "static" attributes. For example,
        when accessing module attributes in protocol checks, or accessing attributes of
        special kinds (like TypeAlias, TypeInfo, etc.) on an instance or class object.
        # TODO: merge with analyze_ref_expr() when we are confident about performance.
        """
        if isinstance(node, (Var, Decorator, OverloadedFuncDef)):
            return node.type or AnyType(TypeOfAny.special_form)
        elif isinstance(node, FuncDef):
            return function_type(node, self.named_type("builtins.function"))
        elif isinstance(node, TypeInfo):
            # Reference to a type object.
            if node.typeddict_type:
                # We special-case TypedDict, because they don't define any constructor.
                return self.typeddict_callable(node)
            elif node.fullname == "types.NoneType":
                # We special case NoneType, because its stub definition is not related to None.
                return TypeType(NoneType())
            else:
                return type_object_type(node, self.named_type)
        elif isinstance(node, TypeAlias):
            # Something that refers to a type alias appears in runtime context.
            # Note that we suppress bogus errors for alias redefinitions,
            # they are already reported in semanal.py.
            with self.msg.filter_errors() if suppress_errors else nullcontext():
                return self.alias_type_in_runtime_context(
                    node, ctx=ctx, alias_definition=is_lvalue
                )
        elif isinstance(node, TypeVarExpr):
            return self.named_type("typing.TypeVar")
        elif isinstance(node, (ParamSpecExpr, TypeVarTupleExpr)):
            return self.object_type()
        elif isinstance(node, MypyFile):
            # Reference to a module object.
            return self.module_type(node) if include_modules else AnyType(TypeOfAny.special_form)
        return AnyType(TypeOfAny.from_error)

    def analyze_var_ref(self, var: Var, context: Context) -> Type:
        if var.type:
            var_type = get_proper_type(var.type)
            if isinstance(var_type, Instance):
                if var.fullname == "typing.Any":
                    # The typeshed type is 'object'; give a more useful type in runtime context
                    return self.named_type("typing._SpecialForm")
                if self.is_literal_context() and var_type.last_known_value is not None:
                    return var_type.last_known_value
                if var.name in {"True", "False"}:
                    return self.infer_literal_expr_type(var.name == "True", "builtins.bool")
            return var.type
        else:
            if not var.is_ready and self.chk.in_checked_function():
                self.chk.handle_cannot_determine_type(var.name, context)
            # Implicit 'Any' type.
            return AnyType(TypeOfAny.special_form)

    def module_type(self, node: MypyFile) -> Instance:
        try:
            result = self.named_type("types.ModuleType")
        except KeyError:
            # In test cases might 'types' may not be available.
            # Fall back to a dummy 'object' type instead to
            # avoid a crash.
            result = self.named_type("builtins.object")
        module_attrs: dict[str, Type] = {}
        immutable = set()
        for name, n in node.names.items():
            if not n.module_public:
                continue
            if isinstance(n.node, Var) and n.node.is_final:
                immutable.add(name)
            if n.node is None:
                module_attrs[name] = AnyType(TypeOfAny.from_error)
            else:
                # TODO: what to do about nested module references?
                # They are non-trivial because there may be import cycles.
                module_attrs[name] = self.analyze_static_reference(
                    n.node, n.node, False, include_modules=False, suppress_errors=True
                )
        result.extra_attrs = ExtraAttrs(module_attrs, immutable, node.fullname)
        return result

    def visit_call_expr(self, e: CallExpr, allow_none_return: bool = False) -> Type:
        """Type check a call expression."""
        if e.analyzed:
            if isinstance(e.analyzed, NamedTupleExpr) and not e.analyzed.is_typed:
                # Type check the arguments, but ignore the results. This relies
                # on the typeshed stubs to type check the arguments.
                self.visit_call_expr_inner(e)
            # It's really a special form that only looks like a call.
            return self.accept(e.analyzed, self.type_context[-1])
        return self.visit_call_expr_inner(e, allow_none_return=allow_none_return)

    def refers_to_typeddict(self, base: Expression) -> bool:
        if not isinstance(base, RefExpr):
            return False
        if isinstance(base.node, TypeInfo) and base.node.typeddict_type is not None:
            # Direct reference.
            return True
        return isinstance(base.node, TypeAlias) and isinstance(
            get_proper_type(base.node.target), TypedDictType
        )

    def visit_call_expr_inner(self, e: CallExpr, allow_none_return: bool = False) -> Type:
        if (
            self.refers_to_typeddict(e.callee)
            or isinstance(e.callee, IndexExpr)
            and self.refers_to_typeddict(e.callee.base)
        ):
            typeddict_callable = get_proper_type(self.accept(e.callee, is_callee=True))
            if isinstance(typeddict_callable, CallableType):
                typeddict_type = get_proper_type(typeddict_callable.ret_type)
                assert isinstance(typeddict_type, TypedDictType)
                return self.check_typeddict_call(
                    typeddict_type, e.arg_kinds, e.arg_names, e.args, e, typeddict_callable
                )
        if (
            isinstance(e.callee, NameExpr)
            and e.callee.name in ("isinstance", "issubclass")
            and len(e.args) == 2
        ):
            for typ in mypy.checker.flatten(e.args[1]):
                node = None
                if isinstance(typ, NameExpr):
                    try:
                        node = self.chk.lookup_qualified(typ.name)
                    except KeyError:
                        # Undefined names should already be reported in semantic analysis.
                        pass
                if is_expr_literal_type(typ):
                    self.msg.cannot_use_function_with_type(e.callee.name, "Literal", e)
                    continue
                if node and isinstance(node.node, TypeAlias):
                    target = get_proper_type(node.node.target)
                    if isinstance(target, AnyType):
                        self.msg.cannot_use_function_with_type(e.callee.name, "Any", e)
                        continue
                    if isinstance(target, NoneType):
                        continue
                if (
                    isinstance(typ, IndexExpr)
                    and isinstance(typ.analyzed, (TypeApplication, TypeAliasExpr))
                ) or (
                    isinstance(typ, NameExpr)
                    and node
                    and isinstance(node.node, TypeAlias)
                    and not node.node.no_args
                    and not (
                        isinstance(union_target := get_proper_type(node.node.target), UnionType)
                        and union_target.uses_pep604_syntax
                    )
                ):
                    self.msg.type_arguments_not_allowed(e)
                if isinstance(typ, RefExpr) and isinstance(typ.node, TypeInfo):
                    if typ.node.typeddict_type:
                        self.msg.cannot_use_function_with_type(e.callee.name, "TypedDict", e)
                    elif typ.node.is_newtype:
                        self.msg.cannot_use_function_with_type(e.callee.name, "NewType", e)
        self.try_infer_partial_type(e)
        type_context = None
        if isinstance(e.callee, LambdaExpr):
            formal_to_actual = map_actuals_to_formals(
                e.arg_kinds,
                e.arg_names,
                e.callee.arg_kinds,
                e.callee.arg_names,
                lambda i: self.accept(e.args[i]),
            )

            arg_types = [
                join.join_type_list([self.accept(e.args[j]) for j in formal_to_actual[i]])
                for i in range(len(e.callee.arg_kinds))
            ]
            type_context = CallableType(
                arg_types,
                e.callee.arg_kinds,
                e.callee.arg_names,
                ret_type=self.object_type(),
                fallback=self.named_type("builtins.function"),
            )
        callee_type = get_proper_type(
            self.accept(e.callee, type_context, always_allow_any=True, is_callee=True)
        )

        # Figure out the full name of the callee for plugin lookup.
        object_type = None
        member = None
        fullname = None
        if isinstance(e.callee, RefExpr):
            # There are two special cases where plugins might act:
            # * A "static" reference/alias to a class or function;
            #   get_function_hook() will be invoked for these.
            fullname = e.callee.fullname or None
            if isinstance(e.callee.node, TypeAlias):
                target = get_proper_type(e.callee.node.target)
                if isinstance(target, Instance):
                    fullname = target.type.fullname
            # * Call to a method on object that has a full name (see
            #   method_fullname() for details on supported objects);
            #   get_method_hook() and get_method_signature_hook() will
            #   be invoked for these.
            if (
                not fullname
                and isinstance(e.callee, MemberExpr)
                and self.chk.has_type(e.callee.expr)
            ):
                member = e.callee.name
                object_type = self.chk.lookup_type(e.callee.expr)

        if (
            self.chk.options.disallow_untyped_calls
            and self.chk.in_checked_function()
            and isinstance(callee_type, CallableType)
            and callee_type.implicit
            and callee_type.name != LAMBDA_NAME
        ):
            if fullname is None and member is not None:
                assert object_type is not None
                fullname = self.method_fullname(object_type, member)
            if not fullname or not any(
                fullname == p or fullname.startswith(f"{p}.")
                for p in self.chk.options.untyped_calls_exclude
            ):
                self.msg.untyped_function_call(callee_type, e)

        ret_type = self.check_call_expr_with_callee_type(
            callee_type, e, fullname, object_type, member
        )
        if isinstance(e.callee, RefExpr) and len(e.args) == 2:
            if e.callee.fullname in ("builtins.isinstance", "builtins.issubclass"):
                self.check_runtime_protocol_test(e)
            if e.callee.fullname == "builtins.issubclass":
                self.check_protocol_issubclass(e)
        if isinstance(e.callee, MemberExpr) and e.callee.name == "format":
            self.check_str_format_call(e)
        ret_type = get_proper_type(ret_type)
        if isinstance(ret_type, UnionType):
            ret_type = make_simplified_union(ret_type.items)
        if isinstance(ret_type, UninhabitedType) and not ret_type.ambiguous:
            self.chk.binder.unreachable()
        # Warn on calls to functions that always return None. The check
        # of ret_type is both a common-case optimization and prevents reporting
        # the error in dynamic functions (where it will be Any).
        if (
            not allow_none_return
            and isinstance(ret_type, NoneType)
            and self.always_returns_none(e.callee)
        ):
            self.chk.msg.does_not_return_value(callee_type, e)
            return AnyType(TypeOfAny.from_error)
        return ret_type

    def check_str_format_call(self, e: CallExpr) -> None:
        """More precise type checking for str.format() calls on literals."""
        assert isinstance(e.callee, MemberExpr)
        format_value = None
        if isinstance(e.callee.expr, StrExpr):
            format_value = e.callee.expr.value
        elif self.chk.has_type(e.callee.expr):
            typ = get_proper_type(self.chk.lookup_type(e.callee.expr))
            if (
                isinstance(typ, Instance)
                and typ.type.is_enum
                and isinstance(typ.last_known_value, LiteralType)
                and isinstance(typ.last_known_value.value, str)
            ):
                value_type = typ.type.names[typ.last_known_value.value].type
                if isinstance(value_type, Type):
                    typ = get_proper_type(value_type)
            base_typ = try_getting_literal(typ)
            if isinstance(base_typ, LiteralType) and isinstance(base_typ.value, str):
                format_value = base_typ.value
        if format_value is not None:
            self.strfrm_checker.check_str_format_call(e, format_value)

    def method_fullname(self, object_type: Type, method_name: str) -> str | None:
        """Convert a method name to a fully qualified name, based on the type of the object that
        it is invoked on. Return `None` if the name of `object_type` cannot be determined.
        """
        object_type = get_proper_type(object_type)

        if isinstance(object_type, CallableType) and object_type.is_type_obj():
            # For class method calls, object_type is a callable representing the class object.
            # We "unwrap" it to a regular type, as the class/instance method difference doesn't
            # affect the fully qualified name.
            object_type = get_proper_type(object_type.ret_type)
        elif isinstance(object_type, TypeType):
            object_type = object_type.item

        type_name = None
        if isinstance(object_type, Instance):
            type_name = object_type.type.fullname
        elif isinstance(object_type, (TypedDictType, LiteralType)):
            info = object_type.fallback.type.get_containing_type_info(method_name)
            type_name = info.fullname if info is not None else None
        elif isinstance(object_type, TupleType):
            type_name = tuple_fallback(object_type).type.fullname

        if type_name:
            return f"{type_name}.{method_name}"
        else:
            return None

    def always_returns_none(self, node: Expression) -> bool:
        """Check if `node` refers to something explicitly annotated as only returning None."""
        if isinstance(node, RefExpr):
            if self.defn_returns_none(node.node):
                return True
        if isinstance(node, MemberExpr) and node.node is None:  # instance or class attribute
            typ = get_proper_type(self.chk.lookup_type(node.expr))
            if isinstance(typ, Instance):
                info = typ.type
            elif isinstance(typ, CallableType) and typ.is_type_obj():
                ret_type = get_proper_type(typ.ret_type)
                if isinstance(ret_type, Instance):
                    info = ret_type.type
                else:
                    return False
            else:
                return False
            sym = info.get(node.name)
            if sym and self.defn_returns_none(sym.node):
                return True
        return False

    def defn_returns_none(self, defn: SymbolNode | None) -> bool:
        """Check if `defn` can _only_ return None."""
        if isinstance(defn, FuncDef):
            return isinstance(defn.type, CallableType) and isinstance(
                get_proper_type(defn.type.ret_type), NoneType
            )
        if isinstance(defn, OverloadedFuncDef):
            return all(self.defn_returns_none(item) for item in defn.items)
        if isinstance(defn, Var):
            typ = get_proper_type(defn.type)
            if (
                not defn.is_inferred
                and isinstance(typ, CallableType)
                and isinstance(get_proper_type(typ.ret_type), NoneType)
            ):
                return True
            if isinstance(typ, Instance):
                sym = typ.type.get("__call__")
                if sym and self.defn_returns_none(sym.node):
                    return True
        return False

    def check_runtime_protocol_test(self, e: CallExpr) -> None:
        for expr in mypy.checker.flatten(e.args[1]):
            tp = get_proper_type(self.chk.lookup_type(expr))
            if (
                isinstance(tp, FunctionLike)
                and tp.is_type_obj()
                and tp.type_object().is_protocol
                and not tp.type_object().runtime_protocol
            ):
                self.chk.fail(message_registry.RUNTIME_PROTOCOL_EXPECTED, e)

    def check_protocol_issubclass(self, e: CallExpr) -> None:
        for expr in mypy.checker.flatten(e.args[1]):
            tp = get_proper_type(self.chk.lookup_type(expr))
            if isinstance(tp, FunctionLike) and tp.is_type_obj() and tp.type_object().is_protocol:
                attr_members = non_method_protocol_members(tp.type_object())
                if attr_members:
                    self.chk.msg.report_non_method_protocol(tp.type_object(), attr_members, e)

    def check_typeddict_call(
        self,
        callee: TypedDictType,
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None],
        args: list[Expression],
        context: Context,
        orig_callee: Type | None,
    ) -> Type:
        if args and all(ak in (ARG_NAMED, ARG_STAR2) for ak in arg_kinds):
            # ex: Point(x=42, y=1337, **extras)
            # This is a bit ugly, but this is a price for supporting all possible syntax
            # variants for TypedDict constructors.
            kwargs = zip([StrExpr(n) if n is not None else None for n in arg_names], args)
            result = self.validate_typeddict_kwargs(kwargs=kwargs, callee=callee)
            if result is not None:
                validated_kwargs, always_present_keys = result
                return self.check_typeddict_call_with_kwargs(
                    callee, validated_kwargs, context, orig_callee, always_present_keys
                )
            return AnyType(TypeOfAny.from_error)

        if len(args) == 1 and arg_kinds[0] == ARG_POS:
            unique_arg = args[0]
            if isinstance(unique_arg, DictExpr):
                # ex: Point({'x': 42, 'y': 1337, **extras})
                return self.check_typeddict_call_with_dict(
                    callee, unique_arg.items, context, orig_callee
                )
            if isinstance(unique_arg, CallExpr) and isinstance(unique_arg.analyzed, DictExpr):
                # ex: Point(dict(x=42, y=1337, **extras))
                return self.check_typeddict_call_with_dict(
                    callee, unique_arg.analyzed.items, context, orig_callee
                )

        if not args:
            # ex: EmptyDict()
            return self.check_typeddict_call_with_kwargs(callee, {}, context, orig_callee, set())

        self.chk.fail(message_registry.INVALID_TYPEDDICT_ARGS, context)
        return AnyType(TypeOfAny.from_error)

    def validate_typeddict_kwargs(
        self, kwargs: Iterable[tuple[Expression | None, Expression]], callee: TypedDictType
    ) -> tuple[dict[str, list[Expression]], set[str]] | None:
        # All (actual or mapped from ** unpacks) expressions that can match given key.
        result = defaultdict(list)
        # Keys that are guaranteed to be present no matter what (e.g. for all items of a union)
        always_present_keys = set()
        # Indicates latest encountered ** unpack among items.
        last_star_found = None

        for item_name_expr, item_arg in kwargs:
            if item_name_expr:
                key_type = self.accept(item_name_expr)
                values = try_getting_str_literals(item_name_expr, key_type)
                literal_value = None
                if values and len(values) == 1:
                    literal_value = values[0]
                if literal_value is None:
                    key_context = item_name_expr or item_arg
                    self.chk.fail(
                        message_registry.TYPEDDICT_KEY_MUST_BE_STRING_LITERAL,
                        key_context,
                        code=codes.LITERAL_REQ,
                    )
                    return None
                else:
                    # A directly present key unconditionally shadows all previously found
                    # values from ** items.
                    # TODO: for duplicate keys, type-check all values.
                    result[literal_value] = [item_arg]
                    always_present_keys.add(literal_value)
            else:
                last_star_found = item_arg
                if not self.validate_star_typeddict_item(
                    item_arg, callee, result, always_present_keys
                ):
                    return None
        if self.chk.options.extra_checks and last_star_found is not None:
            absent_keys = []
            for key in callee.items:
                if key not in callee.required_keys and key not in result:
                    absent_keys.append(key)
            if absent_keys:
                # Having an optional key not explicitly declared by a ** unpacked
                # TypedDict is unsafe, it may be an (incompatible) subtype at runtime.
                # TODO: catch the cases where a declared key is overridden by a subsequent
                # ** item without it (and not again overridden with complete ** item).
                self.msg.non_required_keys_absent_with_star(absent_keys, last_star_found)
        return result, always_present_keys

    def validate_star_typeddict_item(
        self,
        item_arg: Expression,
        callee: TypedDictType,
        result: dict[str, list[Expression]],
        always_present_keys: set[str],
    ) -> bool:
        """Update keys/expressions from a ** expression in TypedDict constructor.

        Note `result` and `always_present_keys` are updated in place. Return true if the
        expression `item_arg` may valid in `callee` TypedDict context.
        """
        inferred = get_proper_type(self.accept(item_arg, type_context=callee))
        possible_tds = []
        if isinstance(inferred, TypedDictType):
            possible_tds = [inferred]
        elif isinstance(inferred, UnionType):
            for item in get_proper_types(inferred.relevant_items()):
                if isinstance(item, TypedDictType):
                    possible_tds.append(item)
                elif not self.valid_unpack_fallback_item(item):
                    self.msg.unsupported_target_for_star_typeddict(item, item_arg)
                    return False
        elif not self.valid_unpack_fallback_item(inferred):
            self.msg.unsupported_target_for_star_typeddict(inferred, item_arg)
            return False
        all_keys: set[str] = set()
        for td in possible_tds:
            all_keys |= td.items.keys()
        for key in all_keys:
            arg = TempNode(
                UnionType.make_union([td.items[key] for td in possible_tds if key in td.items])
            )
            arg.set_line(item_arg)
            if all(key in td.required_keys for td in possible_tds):
                always_present_keys.add(key)
                # Always present keys override previously found values. This is done
                # to support use cases like `Config({**defaults, **overrides})`, where
                # some `overrides` types are narrower that types in `defaults`, and
                # former are too wide for `Config`.
                if result[key]:
                    first = result[key][0]
                    if not isinstance(first, TempNode):
                        # We must always preserve any non-synthetic values, so that
                        # we will accept them even if they are shadowed.
                        result[key] = [first, arg]
                    else:
                        result[key] = [arg]
                else:
                    result[key] = [arg]
            else:
                # If this key is not required at least in some item of a union
                # it may not shadow previous item, so we need to type check both.
                result[key].append(arg)
        return True

    def valid_unpack_fallback_item(self, typ: ProperType) -> bool:
        if isinstance(typ, AnyType):
            return True
        if not isinstance(typ, Instance) or not typ.type.has_base("typing.Mapping"):
            return False
        mapped = map_instance_to_supertype(typ, self.chk.lookup_typeinfo("typing.Mapping"))
        return all(isinstance(a, AnyType) for a in get_proper_types(mapped.args))

    def match_typeddict_call_with_dict(
        self,
        callee: TypedDictType,
        kwargs: list[tuple[Expression | None, Expression]],
        context: Context,
    ) -> bool:
        result = self.validate_typeddict_kwargs(kwargs=kwargs, callee=callee)
        if result is not None:
            validated_kwargs, _ = result
            return callee.required_keys <= set(validated_kwargs.keys()) <= set(callee.items.keys())
        else:
            return False

    def check_typeddict_call_with_dict(
        self,
        callee: TypedDictType,
        kwargs: list[tuple[Expression | None, Expression]],
        context: Context,
        orig_callee: Type | None,
    ) -> Type:
        result = self.validate_typeddict_kwargs(kwargs=kwargs, callee=callee)
        if result is not None:
            validated_kwargs, always_present_keys = result
            return self.check_typeddict_call_with_kwargs(
                callee,
                kwargs=validated_kwargs,
                context=context,
                orig_callee=orig_callee,
                always_present_keys=always_present_keys,
            )
        else:
            return AnyType(TypeOfAny.from_error)

    def typeddict_callable(self, info: TypeInfo) -> CallableType:
        """Construct a reasonable type for a TypedDict type in runtime context.

        If it appears as a callee, it will be special-cased anyway, e.g. it is
        also allowed to accept a single positional argument if it is a dict literal.

        Note it is not safe to move this to type_object_type() since it will crash
        on plugin-generated TypedDicts, that may not have the special_alias.
        """
        assert info.special_alias is not None
        target = info.special_alias.target
        assert isinstance(target, ProperType) and isinstance(target, TypedDictType)
        return self.typeddict_callable_from_context(target, info.defn.type_vars)

    def typeddict_callable_from_context(
        self, callee: TypedDictType, variables: Sequence[TypeVarLikeType] | None = None
    ) -> CallableType:
        return CallableType(
            list(callee.items.values()),
            [
                ArgKind.ARG_NAMED if name in callee.required_keys else ArgKind.ARG_NAMED_OPT
                for name in callee.items
            ],
            list(callee.items.keys()),
            callee,
            self.named_type("builtins.type"),
            variables=variables,
            is_bound=True,
        )

    def check_typeddict_call_with_kwargs(
        self,
        callee: TypedDictType,
        kwargs: dict[str, list[Expression]],
        context: Context,
        orig_callee: Type | None,
        always_present_keys: set[str],
    ) -> Type:
        actual_keys = kwargs.keys()
        if callee.to_be_mutated:
            assigned_readonly_keys = actual_keys & callee.readonly_keys
            if assigned_readonly_keys:
                self.msg.readonly_keys_mutated(assigned_readonly_keys, context=context)
        if not (
            callee.required_keys <= always_present_keys and actual_keys <= callee.items.keys()
        ):
            if not (actual_keys <= callee.items.keys()):
                self.msg.unexpected_typeddict_keys(
                    callee,
                    expected_keys=[
                        key
                        for key in callee.items.keys()
                        if key in callee.required_keys or key in actual_keys
                    ],
                    actual_keys=list(actual_keys),
                    context=context,
                )
            if not (callee.required_keys <= always_present_keys):
                self.msg.unexpected_typeddict_keys(
                    callee,
                    expected_keys=[
                        key for key in callee.items.keys() if key in callee.required_keys
                    ],
                    actual_keys=[
                        key for key in always_present_keys if key in callee.required_keys
                    ],
                    context=context,
                )
            if callee.required_keys > actual_keys:
                # found_set is a sub-set of the required_keys
                # This means we're missing some keys and as such, we can't
                # properly type the object
                return AnyType(TypeOfAny.from_error)

        orig_callee = get_proper_type(orig_callee)
        if isinstance(orig_callee, CallableType):
            infer_callee = orig_callee
        else:
            # Try reconstructing from type context.
            if callee.fallback.type.special_alias is not None:
                infer_callee = self.typeddict_callable(callee.fallback.type)
            else:
                # Likely a TypedDict type generated by a plugin.
                infer_callee = self.typeddict_callable_from_context(callee)

        # We don't show any errors, just infer types in a generic TypedDict type,
        # a custom error message will be given below, if there are errors.
        with self.msg.filter_errors(), self.chk.local_type_map():
            orig_ret_type, _ = self.check_callable_call(
                infer_callee,
                # We use first expression for each key to infer type variables of a generic
                # TypedDict. This is a bit arbitrary, but in most cases will work better than
                # trying to infer a union or a join.
                [args[0] for args in kwargs.values()],
                [ArgKind.ARG_NAMED] * len(kwargs),
                context,
                list(kwargs.keys()),
                None,
                None,
                None,
            )

        ret_type = get_proper_type(orig_ret_type)
        if not isinstance(ret_type, TypedDictType):
            # If something went really wrong, type-check call with original type,
            # this may give a better error message.
            ret_type = callee

        for item_name, item_expected_type in ret_type.items.items():
            if item_name in kwargs:
                item_values = kwargs[item_name]
                for item_value in item_values:
                    self.chk.check_simple_assignment(
                        lvalue_type=item_expected_type,
                        rvalue=item_value,
                        context=item_value,
                        msg=ErrorMessage(
                            message_registry.INCOMPATIBLE_TYPES.value, code=codes.TYPEDDICT_ITEM
                        ),
                        lvalue_name=f'TypedDict item "{item_name}"',
                        rvalue_name="expression",
                    )

        return orig_ret_type

    def get_partial_self_var(self, expr: MemberExpr) -> Var | None:
        """Get variable node for a partial self attribute.

        If the expression is not a self attribute, or attribute is not variable,
        or variable is not partial, return None.
        """
        if not (
            isinstance(expr.expr, NameExpr)
            and isinstance(expr.expr.node, Var)
            and expr.expr.node.is_self
        ):
            # Not a self.attr expression.
            return None
        info = self.chk.scope.enclosing_class()
        if not info or expr.name not in info.names:
            # Don't mess with partial types in superclasses.
            return None
        sym = info.names[expr.name]
        if isinstance(sym.node, Var) and isinstance(sym.node.type, PartialType):
            return sym.node
        return None

    # Types and methods that can be used to infer partial types.
    item_args: ClassVar[dict[str, list[str]]] = {
        "builtins.list": ["append"],
        "builtins.set": ["add", "discard"],
    }
    container_args: ClassVar[dict[str, dict[str, list[str]]]] = {
        "builtins.list": {"extend": ["builtins.list"]},
        "builtins.dict": {"update": ["builtins.dict"]},
        "collections.OrderedDict": {"update": ["builtins.dict"]},
        "builtins.set": {"update": ["builtins.set", "builtins.list"]},
    }

    def try_infer_partial_type(self, e: CallExpr) -> None:
        """Try to make partial type precise from a call."""
        if not isinstance(e.callee, MemberExpr):
            return
        callee = e.callee
        if isinstance(callee.expr, RefExpr):
            # Call a method with a RefExpr callee, such as 'x.method(...)'.
            ret = self.get_partial_var(callee.expr)
            if ret is None:
                return
            var, partial_types = ret
            typ = self.try_infer_partial_value_type_from_call(e, callee.name, var)
            # Var may be deleted from partial_types in try_infer_partial_value_type_from_call
            if typ is not None and var in partial_types:
                self.chk.replace_partial_type(var, typ, partial_types)
        elif isinstance(callee.expr, IndexExpr) and isinstance(callee.expr.base, RefExpr):
            # Call 'x[y].method(...)'; may infer type of 'x' if it's a partial defaultdict.
            if callee.expr.analyzed is not None:
                return  # A special form
            base = callee.expr.base
            index = callee.expr.index
            ret = self.get_partial_var(base)
            if ret is None:
                return
            var, partial_types = ret
            partial_type = get_partial_instance_type(var.type)
            if partial_type is None or partial_type.value_type is None:
                return
            value_type = self.try_infer_partial_value_type_from_call(e, callee.name, var)
            if value_type is not None:
                # Infer key type.
                key_type = self.accept(index)
                if mypy.checker.is_valid_inferred_type(key_type, self.chk.options):
                    # Store inferred partial type.
                    assert partial_type.type is not None
                    typename = partial_type.type.fullname
                    new_type = self.chk.named_generic_type(typename, [key_type, value_type])
                    self.chk.replace_partial_type(var, new_type, partial_types)

    def get_partial_var(self, ref: RefExpr) -> tuple[Var, dict[Var, Context]] | None:
        var = ref.node
        if var is None and isinstance(ref, MemberExpr):
            var = self.get_partial_self_var(ref)
        if not isinstance(var, Var):
            return None
        partial_types = self.chk.find_partial_types(var)
        if partial_types is None:
            return None
        return var, partial_types

    def try_infer_partial_value_type_from_call(
        self, e: CallExpr, methodname: str, var: Var
    ) -> Instance | None:
        """Try to make partial type precise from a call such as 'x.append(y)'."""
        if self.chk.current_node_deferred:
            return None
        partial_type = get_partial_instance_type(var.type)
        if partial_type is None:
            return None
        if partial_type.value_type:
            typename = partial_type.value_type.type.fullname
        else:
            assert partial_type.type is not None
            typename = partial_type.type.fullname
        # Sometimes we can infer a full type for a partial List, Dict or Set type.
        # TODO: Don't infer argument expression twice.
        if (
            typename in self.item_args
            and methodname in self.item_args[typename]
            and e.arg_kinds == [ARG_POS]
        ):
            item_type = self.accept(e.args[0])
            if mypy.checker.is_valid_inferred_type(item_type, self.chk.options):
                return self.chk.named_generic_type(typename, [item_type])
        elif (
            typename in self.container_args
            and methodname in self.container_args[typename]
            and e.arg_kinds == [ARG_POS]
        ):
            arg_type = get_proper_type(self.accept(e.args[0]))
            if isinstance(arg_type, Instance):
                arg_typename = arg_type.type.fullname
                if arg_typename in self.container_args[typename][methodname]:
                    if all(
                        mypy.checker.is_valid_inferred_type(item_type, self.chk.options)
                        for item_type in arg_type.args
                    ):
                        return self.chk.named_generic_type(typename, list(arg_type.args))
            elif isinstance(arg_type, AnyType):
                return self.chk.named_type(typename)

        return None

    def apply_function_plugin(
        self,
        callee: CallableType,
        arg_kinds: list[ArgKind],
        arg_types: list[Type],
        arg_names: Sequence[str | None] | None,
        formal_to_actual: list[list[int]],
        args: list[Expression],
        fullname: str,
        object_type: Type | None,
        context: Context,
    ) -> Type:
        """Use special case logic to infer the return type of a specific named function/method.

        Caller must ensure that a plugin hook exists. There are two different cases:

        - If object_type is None, the caller must ensure that a function hook exists
          for fullname.
        - If object_type is not None, the caller must ensure that a method hook exists
          for fullname.

        Return the inferred return type.
        """
        num_formals = len(callee.arg_types)
        formal_arg_types: list[list[Type]] = [[] for _ in range(num_formals)]
        formal_arg_exprs: list[list[Expression]] = [[] for _ in range(num_formals)]
        formal_arg_names: list[list[str | None]] = [[] for _ in range(num_formals)]
        formal_arg_kinds: list[list[ArgKind]] = [[] for _ in range(num_formals)]
        for formal, actuals in enumerate(formal_to_actual):
            for actual in actuals:
                formal_arg_types[formal].append(arg_types[actual])
                formal_arg_exprs[formal].append(args[actual])
                if arg_names:
                    formal_arg_names[formal].append(arg_names[actual])
                else:
                    formal_arg_names[formal].append(None)
                formal_arg_kinds[formal].append(arg_kinds[actual])

        if object_type is None:
            # Apply function plugin
            callback = self.plugin.get_function_hook(fullname)
            assert callback is not None  # Assume that caller ensures this
            return callback(
                FunctionContext(
                    arg_types=formal_arg_types,
                    arg_kinds=formal_arg_kinds,
                    callee_arg_names=callee.arg_names,
                    arg_names=formal_arg_names,
                    default_return_type=callee.ret_type,
                    args=formal_arg_exprs,
                    context=context,
                    api=self.chk,
                )
            )
        else:
            # Apply method plugin
            method_callback = self.plugin.get_method_hook(fullname)
            assert method_callback is not None  # Assume that caller ensures this
            object_type = get_proper_type(object_type)
            return method_callback(
                MethodContext(
                    type=object_type,
                    arg_types=formal_arg_types,
                    arg_kinds=formal_arg_kinds,
                    callee_arg_names=callee.arg_names,
                    arg_names=formal_arg_names,
                    default_return_type=callee.ret_type,
                    args=formal_arg_exprs,
                    context=context,
                    api=self.chk,
                )
            )

    def apply_signature_hook(
        self,
        callee: FunctionLike,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        hook: Callable[[list[list[Expression]], CallableType], FunctionLike],
    ) -> FunctionLike:
        """Helper to apply a signature hook for either a function or method"""
        if isinstance(callee, CallableType):
            num_formals = len(callee.arg_kinds)
            formal_to_actual = map_actuals_to_formals(
                arg_kinds,
                arg_names,
                callee.arg_kinds,
                callee.arg_names,
                lambda i: self.accept(args[i]),
            )
            formal_arg_exprs: list[list[Expression]] = [[] for _ in range(num_formals)]
            for formal, actuals in enumerate(formal_to_actual):
                for actual in actuals:
                    formal_arg_exprs[formal].append(args[actual])
            return hook(formal_arg_exprs, callee)
        else:
            assert isinstance(callee, Overloaded)
            items = []
            for item in callee.items:
                adjusted = self.apply_signature_hook(item, args, arg_kinds, arg_names, hook)
                assert isinstance(adjusted, CallableType)
                items.append(adjusted)
            return Overloaded(items)

    def apply_function_signature_hook(
        self,
        callee: FunctionLike,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        arg_names: Sequence[str | None] | None,
        signature_hook: Callable[[FunctionSigContext], FunctionLike],
    ) -> FunctionLike:
        """Apply a plugin hook that may infer a more precise signature for a function."""
        return self.apply_signature_hook(
            callee,
            args,
            arg_kinds,
            arg_names,
            (lambda args, sig: signature_hook(FunctionSigContext(args, sig, context, self.chk))),
        )

    def apply_method_signature_hook(
        self,
        callee: FunctionLike,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        arg_names: Sequence[str | None] | None,
        object_type: Type,
        signature_hook: Callable[[MethodSigContext], FunctionLike],
    ) -> FunctionLike:
        """Apply a plugin hook that may infer a more precise signature for a method."""
        pobject_type = get_proper_type(object_type)
        return self.apply_signature_hook(
            callee,
            args,
            arg_kinds,
            arg_names,
            (
                lambda args, sig: signature_hook(
                    MethodSigContext(pobject_type, args, sig, context, self.chk)
                )
            ),
        )

    def transform_callee_type(
        self,
        callable_name: str | None,
        callee: Type,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        arg_names: Sequence[str | None] | None = None,
        object_type: Type | None = None,
    ) -> Type:
        """Attempt to determine a more accurate signature for a method call.

        This is done by looking up and applying a method signature hook (if one exists for the
        given method name).

        If no matching method signature hook is found, callee is returned unmodified. The same
        happens if the arguments refer to a non-method callable (this is allowed so that the code
        calling transform_callee_type needs to perform fewer boilerplate checks).

        Note: this method is *not* called automatically as part of check_call, because in some
        cases check_call is called multiple times while checking a single call (for example when
        dealing with overloads). Instead, this method needs to be called explicitly
        (if appropriate) before the signature is passed to check_call.
        """
        callee = get_proper_type(callee)
        if callable_name is not None and isinstance(callee, FunctionLike):
            if object_type is not None:
                method_sig_hook = self.plugin.get_method_signature_hook(callable_name)
                if method_sig_hook:
                    return self.apply_method_signature_hook(
                        callee, args, arg_kinds, context, arg_names, object_type, method_sig_hook
                    )
            else:
                function_sig_hook = self.plugin.get_function_signature_hook(callable_name)
                if function_sig_hook:
                    return self.apply_function_signature_hook(
                        callee, args, arg_kinds, context, arg_names, function_sig_hook
                    )

        return callee

    def is_generic_decorator_overload_call(
        self, callee_type: CallableType, args: list[Expression]
    ) -> Overloaded | None:
        """Check if this looks like an application of a generic function to overload argument."""
        assert callee_type.variables
        if len(callee_type.arg_types) != 1 or len(args) != 1:
            # TODO: can we handle more general cases?
            return None
        if not isinstance(get_proper_type(callee_type.arg_types[0]), CallableType):
            return None
        if not isinstance(get_proper_type(callee_type.ret_type), CallableType):
            return None
        with self.chk.local_type_map():
            with self.msg.filter_errors():
                arg_type = get_proper_type(self.accept(args[0], type_context=None))
        if isinstance(arg_type, Overloaded):
            return arg_type
        return None

    def handle_decorator_overload_call(
        self, callee_type: CallableType, overloaded: Overloaded, ctx: Context
    ) -> tuple[Type, Type] | None:
        """Type-check application of a generic callable to an overload.

        We check call on each individual overload item, and then combine results into a new
        overload. This function should be only used if callee_type takes and returns a Callable.
        """
        result = []
        inferred_args = []
        for item in overloaded.items:
            arg = TempNode(typ=item)
            with self.msg.filter_errors() as err:
                item_result, inferred_arg = self.check_call(callee_type, [arg], [ARG_POS], ctx)
            if err.has_new_errors():
                # This overload doesn't match.
                continue
            p_item_result = get_proper_type(item_result)
            if not isinstance(p_item_result, CallableType):
                continue
            p_inferred_arg = get_proper_type(inferred_arg)
            if not isinstance(p_inferred_arg, CallableType):
                continue
            inferred_args.append(p_inferred_arg)
            result.append(p_item_result)
        if not result or not inferred_args:
            # None of the overload matched (or overload was initially malformed).
            return None
        return Overloaded(result), Overloaded(inferred_args)

    def check_call_expr_with_callee_type(
        self,
        callee_type: Type,
        e: CallExpr,
        callable_name: str | None,
        object_type: Type | None,
        member: str | None = None,
    ) -> Type:
        """Type check call expression.

        The callee_type should be used as the type of callee expression. In particular,
        in case of a union type this can be a particular item of the union, so that we can
        apply plugin hooks to each item.

        The 'member', 'callable_name' and 'object_type' are only used to call plugin hooks.
        If 'callable_name' is None but 'member' is not None (member call), try constructing
        'callable_name' using 'object_type' (the base type on which the method is called),
        for example 'typing.Mapping.get'.
        """
        if callable_name is None and member is not None:
            assert object_type is not None
            callable_name = self.method_fullname(object_type, member)
        object_type = get_proper_type(object_type)
        if callable_name:
            # Try to refine the call signature using plugin hooks before checking the call.
            callee_type = self.transform_callee_type(
                callable_name, callee_type, e.args, e.arg_kinds, e, e.arg_names, object_type
            )
        # Unions are special-cased to allow plugins to act on each item in the union.
        elif member is not None and isinstance(object_type, UnionType):
            return self.check_union_call_expr(e, object_type, member)
        ret_type, callee_type = self.check_call(
            callee_type,
            e.args,
            e.arg_kinds,
            e,
            e.arg_names,
            callable_node=e.callee,
            callable_name=callable_name,
            object_type=object_type,
        )
        proper_callee = get_proper_type(callee_type)
        if isinstance(e.callee, (NameExpr, MemberExpr)):
            node = e.callee.node
            if node is None and member is not None and isinstance(object_type, Instance):
                if (symbol := object_type.type.get(member)) is not None:
                    node = symbol.node
            self.chk.check_deprecated(node, e)
            self.chk.warn_deprecated_overload_item(
                node, e, target=callee_type, selftype=object_type
            )
        if isinstance(e.callee, RefExpr) and isinstance(proper_callee, CallableType):
            # Cache it for find_isinstance_check()
            if proper_callee.type_guard is not None:
                e.callee.type_guard = proper_callee.type_guard
            if proper_callee.type_is is not None:
                e.callee.type_is = proper_callee.type_is
        return ret_type

    def check_union_call_expr(self, e: CallExpr, object_type: UnionType, member: str) -> Type:
        """Type check calling a member expression where the base type is a union."""
        res: list[Type] = []
        for typ in object_type.relevant_items():
            # Member access errors are already reported when visiting the member expression.
            with self.msg.filter_errors():
                item = analyze_member_access(
                    member,
                    typ,
                    e,
                    is_lvalue=False,
                    is_super=False,
                    is_operator=False,
                    original_type=object_type,
                    chk=self.chk,
                    in_literal_context=self.is_literal_context(),
                    self_type=typ,
                )
            narrowed = self.narrow_type_from_binder(e.callee, item, skip_non_overlapping=True)
            if narrowed is None:
                continue
            callable_name = self.method_fullname(typ, member)
            item_object_type = typ if callable_name else None
            res.append(
                self.check_call_expr_with_callee_type(narrowed, e, callable_name, item_object_type)
            )
        return make_simplified_union(res)

    def check_call(
        self,
        callee: Type,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        arg_names: Sequence[str | None] | None = None,
        callable_node: Expression | None = None,
        callable_name: str | None = None,
        object_type: Type | None = None,
        original_type: Type | None = None,
    ) -> tuple[Type, Type]:
        """Type check a call.

        Also infer type arguments if the callee is a generic function.

        Return (result type, inferred callee type).

        Arguments:
            callee: type of the called value
            args: actual argument expressions
            arg_kinds: contains nodes.ARG_* constant for each argument in args
                 describing whether the argument is positional, *arg, etc.
            context: current expression context, used for inference.
            arg_names: names of arguments (optional)
            callable_node: associate the inferred callable type to this node,
                if specified
            callable_name: Fully-qualified name of the function/method to call,
                or None if unavailable (examples: 'builtins.open', 'typing.Mapping.get')
            object_type: If callable_name refers to a method, the type of the object
                on which the method is being called
        """
        callee = get_proper_type(callee)

        if isinstance(callee, CallableType):
            if callee.variables:
                overloaded = self.is_generic_decorator_overload_call(callee, args)
                if overloaded is not None:
                    # Special casing for inline application of generic callables to overloads.
                    # Supporting general case would be tricky, but this should cover 95% of cases.
                    overloaded_result = self.handle_decorator_overload_call(
                        callee, overloaded, context
                    )
                    if overloaded_result is not None:
                        return overloaded_result

            return self.check_callable_call(
                callee,
                args,
                arg_kinds,
                context,
                arg_names,
                callable_node,
                callable_name,
                object_type,
            )
        elif isinstance(callee, Overloaded):
            return self.check_overload_call(
                callee, args, arg_kinds, arg_names, callable_name, object_type, context
            )
        elif isinstance(callee, AnyType) or not self.chk.in_checked_function():
            return self.check_any_type_call(args, callee)
        elif isinstance(callee, UnionType):
            return self.check_union_call(callee, args, arg_kinds, arg_names, context)
        elif isinstance(callee, Instance):
            call_function = analyze_member_access(
                "__call__",
                callee,
                context,
                is_lvalue=False,
                is_super=False,
                is_operator=True,
                original_type=original_type or callee,
                chk=self.chk,
                in_literal_context=self.is_literal_context(),
            )
            callable_name = callee.type.fullname + ".__call__"
            # Apply method signature hook, if one exists
            call_function = self.transform_callee_type(
                callable_name, call_function, args, arg_kinds, context, arg_names, callee
            )
            result = self.check_call(
                call_function,
                args,
                arg_kinds,
                context,
                arg_names,
                callable_node,
                callable_name,
                callee,
            )
            if callable_node:
                # check_call() stored "call_function" as the type, which is incorrect.
                # Override the type.
                self.chk.store_type(callable_node, callee)
            return result
        elif isinstance(callee, TypeVarType):
            return self.check_call(
                callee.upper_bound, args, arg_kinds, context, arg_names, callable_node
            )
        elif isinstance(callee, TypeType):
            item = self.analyze_type_type_callee(callee.item, context)
            return self.check_call(item, args, arg_kinds, context, arg_names, callable_node)
        elif isinstance(callee, TupleType):
            return self.check_call(
                tuple_fallback(callee),
                args,
                arg_kinds,
                context,
                arg_names,
                callable_node,
                callable_name,
                object_type,
                original_type=callee,
            )
        elif isinstance(callee, UninhabitedType):
            ret = UninhabitedType()
            ret.ambiguous = callee.ambiguous
            return callee, ret
        else:
            return self.msg.not_callable(callee, context), AnyType(TypeOfAny.from_error)

    def check_callable_call(
        self,
        callee: CallableType,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        arg_names: Sequence[str | None] | None,
        callable_node: Expression | None,
        callable_name: str | None,
        object_type: Type | None,
    ) -> tuple[Type, Type]:
        """Type check a call that targets a callable value.

        See the docstring of check_call for more information.
        """
        # Always unpack **kwargs before checking a call.
        callee = callee.with_unpacked_kwargs().with_normalized_var_args()
        if callable_name is None and callee.name:
            callable_name = callee.name
        ret_type = get_proper_type(callee.ret_type)
        if callee.is_type_obj() and isinstance(ret_type, Instance):
            callable_name = ret_type.type.fullname
        if isinstance(callable_node, RefExpr) and callable_node.fullname in ENUM_BASES:
            # An Enum() call that failed SemanticAnalyzerPass2.check_enum_call().
            return callee.ret_type, callee

        if (
            callee.is_type_obj()
            and callee.type_object().is_protocol
            # Exception for Type[...]
            and not callee.from_type_type
        ):
            self.chk.fail(
                message_registry.CANNOT_INSTANTIATE_PROTOCOL.format(callee.type_object().name),
                context,
            )
        elif (
            callee.is_type_obj()
            and callee.type_object().is_abstract
            # Exception for Type[...]
            and not callee.from_type_type
            and not callee.type_object().fallback_to_any
        ):
            type = callee.type_object()
            # Determine whether the implicitly abstract attributes are functions with
            # None-compatible return types.
            abstract_attributes: dict[str, bool] = {}
            for attr_name, abstract_status in type.abstract_attributes:
                if abstract_status == IMPLICITLY_ABSTRACT:
                    abstract_attributes[attr_name] = self.can_return_none(type, attr_name)
                else:
                    abstract_attributes[attr_name] = False
            self.msg.cannot_instantiate_abstract_class(
                callee.type_object().name, abstract_attributes, context
            )

        var_arg = callee.var_arg()
        if var_arg and isinstance(var_arg.typ, UnpackType):
            # It is hard to support multiple variadic unpacks (except for old-style *args: int),
            # fail gracefully to avoid crashes later.
            seen_unpack = False
            for arg, arg_kind in zip(args, arg_kinds):
                if arg_kind != ARG_STAR:
                    continue
                arg_type = get_proper_type(self.accept(arg))
                if not isinstance(arg_type, TupleType) or any(
                    isinstance(t, UnpackType) for t in arg_type.items
                ):
                    if seen_unpack:
                        self.msg.fail(
                            "Passing multiple variadic unpacks in a call is not supported",
                            context,
                            code=codes.CALL_ARG,
                        )
                        return AnyType(TypeOfAny.from_error), callee
                    seen_unpack = True

        # This is tricky: return type may contain its own type variables, like in
        # def [S] (S) -> def [T] (T) -> tuple[S, T], so we need to update their ids
        # to avoid possible id clashes if this call itself appears in a generic
        # function body.
        ret_type = get_proper_type(callee.ret_type)
        if isinstance(ret_type, CallableType) and ret_type.variables:
            fresh_ret_type = freshen_all_functions_type_vars(callee.ret_type)
            freeze_all_type_vars(fresh_ret_type)
            callee = callee.copy_modified(ret_type=fresh_ret_type)

        if callee.is_generic():
            callee = freshen_function_type_vars(callee)
            callee = self.infer_function_type_arguments_using_context(callee, context)

        formal_to_actual = map_actuals_to_formals(
            arg_kinds,
            arg_names,
            callee.arg_kinds,
            callee.arg_names,
            lambda i: self.accept(args[i]),
        )

        if callee.is_generic():
            need_refresh = any(
                isinstance(v, (ParamSpecType, TypeVarTupleType)) for v in callee.variables
            )
            callee = self.infer_function_type_arguments(
                callee, args, arg_kinds, arg_names, formal_to_actual, need_refresh, context
            )
            if need_refresh:
                # Argument kinds etc. may have changed due to
                # ParamSpec or TypeVarTuple variables being replaced with an arbitrary
                # number of arguments; recalculate actual-to-formal map
                formal_to_actual = map_actuals_to_formals(
                    arg_kinds,
                    arg_names,
                    callee.arg_kinds,
                    callee.arg_names,
                    lambda i: self.accept(args[i]),
                )

        param_spec = callee.param_spec()
        if (
            param_spec is not None
            and arg_kinds == [ARG_STAR, ARG_STAR2]
            and len(formal_to_actual) == 2
        ):
            arg1 = self.accept(args[0])
            arg2 = self.accept(args[1])
            if (
                isinstance(arg1, ParamSpecType)
                and isinstance(arg2, ParamSpecType)
                and arg1.flavor == ParamSpecFlavor.ARGS
                and arg2.flavor == ParamSpecFlavor.KWARGS
                and arg1.id == arg2.id == param_spec.id
            ):
                return callee.ret_type, callee

        arg_types = self.infer_arg_types_in_context(callee, args, arg_kinds, formal_to_actual)

        self.check_argument_count(
            callee,
            arg_types,
            arg_kinds,
            arg_names,
            formal_to_actual,
            context,
            object_type,
            callable_name,
        )

        self.check_argument_types(
            arg_types, arg_kinds, args, callee, formal_to_actual, context, object_type=object_type
        )

        if (
            callee.is_type_obj()
            and (len(arg_types) == 1)
            and is_equivalent(callee.ret_type, self.named_type("builtins.type"))
        ):
            callee = callee.copy_modified(ret_type=TypeType.make_normalized(arg_types[0]))

        if callable_node:
            # Store the inferred callable type.
            self.chk.store_type(callable_node, callee)

        if callable_name and (
            (object_type is None and self.plugin.get_function_hook(callable_name))
            or (object_type is not None and self.plugin.get_method_hook(callable_name))
        ):
            new_ret_type = self.apply_function_plugin(
                callee,
                arg_kinds,
                arg_types,
                arg_names,
                formal_to_actual,
                args,
                callable_name,
                object_type,
                context,
            )
            callee = callee.copy_modified(ret_type=new_ret_type)
        return callee.ret_type, callee

    def can_return_none(self, type: TypeInfo, attr_name: str) -> bool:
        """Is the given attribute a method with a None-compatible return type?

        Overloads are only checked if there is an implementation.
        """
        if not state.strict_optional:
            # If strict-optional is not set, is_subtype(NoneType(), T) is always True.
            # So, we cannot do anything useful here in that case.
            return False
        for base in type.mro:
            symnode = base.names.get(attr_name)
            if symnode is None:
                continue
            node = symnode.node
            if isinstance(node, OverloadedFuncDef):
                node = node.impl
            if isinstance(node, Decorator):
                node = node.func
            if isinstance(node, FuncDef):
                if node.type is not None:
                    assert isinstance(node.type, CallableType)
                    return is_subtype(NoneType(), node.type.ret_type)
        return False

    def analyze_type_type_callee(self, item: ProperType, context: Context) -> Type:
        """Analyze the callee X in X(...) where X is Type[item].

        Return a Y that we can pass to check_call(Y, ...).
        """
        if isinstance(item, AnyType):
            return AnyType(TypeOfAny.from_another_any, source_any=item)
        if isinstance(item, Instance):
            res = type_object_type(item.type, self.named_type)
            if isinstance(res, CallableType):
                res = res.copy_modified(from_type_type=True)
            expanded = expand_type_by_instance(res, item)
            if isinstance(expanded, CallableType):
                # Callee of the form Type[...] should never be generic, only
                # proper class objects can be.
                expanded = expanded.copy_modified(variables=[])
            return expanded
        if isinstance(item, UnionType):
            return UnionType(
                [
                    self.analyze_type_type_callee(get_proper_type(tp), context)
                    for tp in item.relevant_items()
                ],
                item.line,
            )
        if isinstance(item, TypeVarType):
            # Pretend we're calling the typevar's upper bound,
            # i.e. its constructor (a poor approximation for reality,
            # but better than AnyType...), but replace the return type
            # with typevar.
            callee = self.analyze_type_type_callee(get_proper_type(item.upper_bound), context)
            callee = get_proper_type(callee)
            if isinstance(callee, CallableType):
                callee = callee.copy_modified(ret_type=item)
            elif isinstance(callee, Overloaded):
                callee = Overloaded([c.copy_modified(ret_type=item) for c in callee.items])
            return callee
        # We support Type of namedtuples but not of tuples in general
        if isinstance(item, TupleType) and tuple_fallback(item).type.fullname != "builtins.tuple":
            return self.analyze_type_type_callee(tuple_fallback(item), context)
        if isinstance(item, TypedDictType):
            return self.typeddict_callable_from_context(item)

        self.msg.unsupported_type_type(item, context)
        return AnyType(TypeOfAny.from_error)

    def infer_arg_types_in_empty_context(self, args: list[Expression]) -> list[Type]:
        """Infer argument expression types in an empty context.

        In short, we basically recurse on each argument without considering
        in what context the argument was called.
        """
        res: list[Type] = []

        for arg in args:
            arg_type = self.accept(arg)
            if has_erased_component(arg_type):
                res.append(NoneType())
            else:
                res.append(arg_type)
        return res

    def infer_more_unions_for_recursive_type(self, type_context: Type) -> bool:
        """Adjust type inference of unions if type context has a recursive type.

        Return the old state. The caller must assign it to type_state.infer_unions
        afterwards.

        This is a hack to better support inference for recursive types.

        Note: This is performance-sensitive and must not be a context manager
        until mypyc supports them better.
        """
        old = type_state.infer_unions
        if has_recursive_types(type_context):
            type_state.infer_unions = True
        return old

    def infer_arg_types_in_context(
        self,
        callee: CallableType,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        formal_to_actual: list[list[int]],
    ) -> list[Type]:
        """Infer argument expression types using a callable type as context.

        For example, if callee argument 2 has type List[int], infer the
        argument expression with List[int] type context.

        Returns the inferred types of *actual arguments*.
        """
        res: list[Type | None] = [None] * len(args)

        for i, actuals in enumerate(formal_to_actual):
            for ai in actuals:
                if not arg_kinds[ai].is_star():
                    arg_type = callee.arg_types[i]
                    # When the outer context for a function call is known to be recursive,
                    # we solve type constraints inferred from arguments using unions instead
                    # of joins. This is a bit arbitrary, but in practice it works for most
                    # cases. A cleaner alternative would be to switch to single bin type
                    # inference, but this is a lot of work.
                    old = self.infer_more_unions_for_recursive_type(arg_type)
                    res[ai] = self.accept(args[ai], arg_type)
                    # We need to manually restore union inference state, ugh.
                    type_state.infer_unions = old

        # Fill in the rest of the argument types.
        for i, t in enumerate(res):
            if not t:
                res[i] = self.accept(args[i])
        assert all(tp is not None for tp in res)
        return cast(list[Type], res)

    def infer_function_type_arguments_using_context(
        self, callable: CallableType, error_context: Context
    ) -> CallableType:
        """Unify callable return type to type context to infer type vars.

        For example, if the return type is set[t] where 't' is a type variable
        of callable, and if the context is set[int], return callable modified
        by substituting 't' with 'int'.
        """
        ctx = self.type_context[-1]
        if not ctx:
            return callable
        # The return type may have references to type metavariables that
        # we are inferring right now. We must consider them as indeterminate
        # and they are not potential results; thus we replace them with the
        # special ErasedType type. On the other hand, class type variables are
        # valid results.
        erased_ctx = replace_meta_vars(ctx, ErasedType())
        ret_type = callable.ret_type
        if is_overlapping_none(ret_type) and is_overlapping_none(ctx):
            # If both the context and the return type are optional, unwrap the optional,
            # since in 99% cases this is what a user expects. In other words, we replace
            #     Optional[T] <: Optional[int]
            # with
            #     T <: int
            # while the former would infer T <: Optional[int].
            ret_type = remove_optional(ret_type)
            erased_ctx = remove_optional(erased_ctx)
            #
            # TODO: Instead of this hack and the one below, we need to use outer and
            # inner contexts at the same time. This is however not easy because of two
            # reasons:
            #   * We need to support constraints like [1 <: 2, 2 <: X], i.e. with variables
            #     on both sides. (This is not too hard.)
            #   * We need to update all the inference "infrastructure", so that all
            #     variables in an expression are inferred at the same time.
            #     (And this is hard, also we need to be careful with lambdas that require
            #     two passes.)
        proper_ret = get_proper_type(ret_type)
        if (
            isinstance(proper_ret, TypeVarType)
            or isinstance(proper_ret, UnionType)
            and all(isinstance(get_proper_type(u), TypeVarType) for u in proper_ret.items)
        ):
            # Another special case: the return type is a type variable. If it's unrestricted,
            # we could infer a too general type for the type variable if we use context,
            # and this could result in confusing and spurious type errors elsewhere.
            #
            # So we give up and just use function arguments for type inference, with just two
            # exceptions:
            #
            # 1. If the context is a generic instance type, actually use it as context, as
            #    this *seems* to usually be the reasonable thing to do.
            #
            #    See also github issues #462 and #360.
            #
            # 2. If the context is some literal type, we want to "propagate" that information
            #    down so that we infer a more precise type for literal expressions. For example,
            #    the expression `3` normally has an inferred type of `builtins.int`: but if it's
            #    in a literal context like below, we want it to infer `Literal[3]` instead.
            #
            #        def expects_literal(x: Literal[3]) -> None: pass
            #        def identity(x: T) -> T: return x
            #
            #        expects_literal(identity(3))  # Should type-check
            # TODO: we may want to add similar exception if all arguments are lambdas, since
            # in this case external context is almost everything we have.
            if not is_generic_instance(ctx) and not is_literal_type_like(ctx):
                return callable.copy_modified()
        args = infer_type_arguments(
            callable.variables, ret_type, erased_ctx, skip_unsatisfied=True
        )
        # Only substitute non-Uninhabited and non-erased types.
        new_args: list[Type | None] = []
        for arg in args:
            if has_uninhabited_component(arg) or has_erased_component(arg):
                new_args.append(None)
            else:
                new_args.append(arg)
        # Don't show errors after we have only used the outer context for inference.
        # We will use argument context to infer more variables.
        return self.apply_generic_arguments(
            callable, new_args, error_context, skip_unsatisfied=True
        )

    def infer_function_type_arguments(
        self,
        callee_type: CallableType,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        formal_to_actual: list[list[int]],
        need_refresh: bool,
        context: Context,
    ) -> CallableType:
        """Infer the type arguments for a generic callee type.

        Infer based on the types of arguments.

        Return a derived callable type that has the arguments applied.
        """
        if self.chk.in_checked_function():
            # Disable type errors during type inference. There may be errors
            # due to partial available context information at this time, but
            # these errors can be safely ignored as the arguments will be
            # inferred again later.
            with self.msg.filter_errors():
                arg_types = self.infer_arg_types_in_context(
                    callee_type, args, arg_kinds, formal_to_actual
                )

            arg_pass_nums = self.get_arg_infer_passes(
                callee_type, args, arg_types, formal_to_actual, len(args)
            )

            pass1_args: list[Type | None] = []
            for i, arg in enumerate(arg_types):
                if arg_pass_nums[i] > 1:
                    pass1_args.append(None)
                else:
                    pass1_args.append(arg)

            inferred_args, _ = infer_function_type_arguments(
                callee_type,
                pass1_args,
                arg_kinds,
                arg_names,
                formal_to_actual,
                context=self.argument_infer_context(),
                strict=self.chk.in_checked_function(),
            )

            if 2 in arg_pass_nums:
                # Second pass of type inference.
                (callee_type, inferred_args) = self.infer_function_type_arguments_pass2(
                    callee_type,
                    args,
                    arg_kinds,
                    arg_names,
                    formal_to_actual,
                    inferred_args,
                    need_refresh,
                    context,
                )

            if (
                callee_type.special_sig == "dict"
                and len(inferred_args) == 2
                and (ARG_NAMED in arg_kinds or ARG_STAR2 in arg_kinds)
            ):
                # HACK: Infer str key type for dict(...) with keyword args. The type system
                #       can't represent this so we special case it, as this is a pretty common
                #       thing. This doesn't quite work with all possible subclasses of dict
                #       if they shuffle type variables around, as we assume that there is a 1-1
                #       correspondence with dict type variables. This is a marginal issue and
                #       a little tricky to fix so it's left unfixed for now.
                first_arg = get_proper_type(inferred_args[0])
                if isinstance(first_arg, (NoneType, UninhabitedType)):
                    inferred_args[0] = self.named_type("builtins.str")
                elif not first_arg or not is_subtype(self.named_type("builtins.str"), first_arg):
                    self.chk.fail(message_registry.KEYWORD_ARGUMENT_REQUIRES_STR_KEY_TYPE, context)

            if not self.chk.options.old_type_inference and any(
                a is None
                or isinstance(get_proper_type(a), UninhabitedType)
                or set(get_type_vars(a)) & set(callee_type.variables)
                for a in inferred_args
            ):
                if need_refresh:
                    # Technically we need to refresh formal_to_actual after *each* inference pass,
                    # since each pass can expand ParamSpec or TypeVarTuple. Although such situations
                    # are very rare, not doing this can cause crashes.
                    formal_to_actual = map_actuals_to_formals(
                        arg_kinds,
                        arg_names,
                        callee_type.arg_kinds,
                        callee_type.arg_names,
                        lambda a: self.accept(args[a]),
                    )
                # If the regular two-phase inference didn't work, try inferring type
                # variables while allowing for polymorphic solutions, i.e. for solutions
                # potentially involving free variables.
                # TODO: support the similar inference for return type context.
                poly_inferred_args, free_vars = infer_function_type_arguments(
                    callee_type,
                    arg_types,
                    arg_kinds,
                    arg_names,
                    formal_to_actual,
                    context=self.argument_infer_context(),
                    strict=self.chk.in_checked_function(),
                    allow_polymorphic=True,
                )
                poly_callee_type = self.apply_generic_arguments(
                    callee_type, poly_inferred_args, context
                )
                # Try applying inferred polymorphic type if possible, e.g. Callable[[T], T] can
                # be interpreted as def [T] (T) -> T, but dict[T, T] cannot be expressed.
                applied = applytype.apply_poly(poly_callee_type, free_vars)
                if applied is not None and all(
                    a is not None and not isinstance(get_proper_type(a), UninhabitedType)
                    for a in poly_inferred_args
                ):
                    freeze_all_type_vars(applied)
                    return applied
                # If it didn't work, erase free variables as uninhabited, to avoid confusing errors.
                unknown = UninhabitedType()
                unknown.ambiguous = True
                inferred_args = [
                    (
                        expand_type(
                            a, {v.id: unknown for v in list(callee_type.variables) + free_vars}
                        )
                        if a is not None
                        else None
                    )
                    for a in poly_inferred_args
                ]
        else:
            # In dynamically typed functions use implicit 'Any' types for
            # type variables.
            inferred_args = [AnyType(TypeOfAny.unannotated)] * len(callee_type.variables)
        return self.apply_inferred_arguments(callee_type, inferred_args, context)

    def infer_function_type_arguments_pass2(
        self,
        callee_type: CallableType,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        formal_to_actual: list[list[int]],
        old_inferred_args: Sequence[Type | None],
        need_refresh: bool,
        context: Context,
    ) -> tuple[CallableType, list[Type | None]]:
        """Perform second pass of generic function type argument inference.

        The second pass is needed for arguments with types such as Callable[[T], S],
        where both T and S are type variables, when the actual argument is a
        lambda with inferred types.  The idea is to infer the type variable T
        in the first pass (based on the types of other arguments).  This lets
        us infer the argument and return type of the lambda expression and
        thus also the type variable S in this second pass.

        Return (the callee with type vars applied, inferred actual arg types).
        """
        # None or erased types in inferred types mean that there was not enough
        # information to infer the argument. Replace them with None values so
        # that they are not applied yet below.
        inferred_args = list(old_inferred_args)
        for i, arg in enumerate(get_proper_types(inferred_args)):
            if isinstance(arg, (NoneType, UninhabitedType)) or has_erased_component(arg):
                inferred_args[i] = None
        callee_type = self.apply_generic_arguments(callee_type, inferred_args, context)

        if not callee_type.is_generic():
            # Fast path, second pass can't give new information.
            return callee_type, []

        if need_refresh:
            formal_to_actual = map_actuals_to_formals(
                arg_kinds,
                arg_names,
                callee_type.arg_kinds,
                callee_type.arg_names,
                lambda a: self.accept(args[a]),
            )

        # Same as during first pass, disable type errors (we still have partial context).
        with self.msg.filter_errors():
            arg_types = self.infer_arg_types_in_context(
                callee_type, args, arg_kinds, formal_to_actual
            )

        inferred_args, _ = infer_function_type_arguments(
            callee_type,
            arg_types,
            arg_kinds,
            arg_names,
            formal_to_actual,
            context=self.argument_infer_context(),
        )

        return callee_type, inferred_args

    def argument_infer_context(self) -> ArgumentInferContext:
        if self._arg_infer_context_cache is None:
            self._arg_infer_context_cache = ArgumentInferContext(
                self.chk.named_type("typing.Mapping"), self.chk.named_type("typing.Iterable")
            )
        return self._arg_infer_context_cache

    def get_arg_infer_passes(
        self,
        callee: CallableType,
        args: list[Expression],
        arg_types: list[Type],
        formal_to_actual: list[list[int]],
        num_actuals: int,
    ) -> list[int]:
        """Return pass numbers for args for two-pass argument type inference.

        For each actual, the pass number is either 1 (first pass) or 2 (second
        pass).

        Two-pass argument type inference primarily lets us infer types of
        lambdas more effectively.
        """
        res = [1] * num_actuals
        for i, arg in enumerate(callee.arg_types):
            skip_param_spec = False
            p_formal = get_proper_type(callee.arg_types[i])
            if isinstance(p_formal, CallableType) and p_formal.param_spec():
                for j in formal_to_actual[i]:
                    p_actual = get_proper_type(arg_types[j])
                    # This is an exception from the usual logic where we put generic Callable
                    # arguments in the second pass. If we have a non-generic actual, it is
                    # likely to infer good constraints, for example if we have:
                    #   def run(Callable[P, None], *args: P.args, **kwargs: P.kwargs) -> None: ...
                    #   def test(x: int, y: int) -> int: ...
                    #   run(test, 1, 2)
                    # we will use `test` for inference, since it will allow to infer also
                    # argument *names* for P <: [x: int, y: int].
                    if isinstance(p_actual, Instance):
                        call_method = find_member("__call__", p_actual, p_actual, is_operator=True)
                        if call_method is not None:
                            p_actual = get_proper_type(call_method)
                    if (
                        isinstance(p_actual, CallableType)
                        and not p_actual.variables
                        and not isinstance(args[j], LambdaExpr)
                    ):
                        skip_param_spec = True
                        break
            if not skip_param_spec and arg.accept(ArgInferSecondPassQuery()):
                for j in formal_to_actual[i]:
                    res[j] = 2
        return res

    def apply_inferred_arguments(
        self, callee_type: CallableType, inferred_args: Sequence[Type | None], context: Context
    ) -> CallableType:
        """Apply inferred values of type arguments to a generic function.

        Inferred_args contains the values of function type arguments.
        """
        # Report error if some of the variables could not be solved. In that
        # case assume that all variables have type Any to avoid extra
        # bogus error messages.
        for inferred_type, tv in zip(inferred_args, callee_type.variables):
            if not inferred_type or has_erased_component(inferred_type):
                # Could not infer a non-trivial type for a type variable.
                self.msg.could_not_infer_type_arguments(callee_type, tv, context)
                inferred_args = [AnyType(TypeOfAny.from_error)] * len(inferred_args)
        # Apply the inferred types to the function type. In this case the
        # return type must be CallableType, since we give the right number of type
        # arguments.
        return self.apply_generic_arguments(callee_type, inferred_args, context)

    def check_argument_count(
        self,
        callee: CallableType,
        actual_types: list[Type],
        actual_kinds: list[ArgKind],
        actual_names: Sequence[str | None] | None,
        formal_to_actual: list[list[int]],
        context: Context | None,
        object_type: Type | None = None,
        callable_name: str | None = None,
    ) -> bool:
        """Check that there is a value for all required arguments to a function.

        Also check that there are no duplicate values for arguments. Report found errors
        using 'messages' if it's not None. If 'messages' is given, 'context' must also be given.

        Return False if there were any errors. Otherwise return True
        """
        if context is None:
            # Avoid "is None" checks
            context = TempNode(AnyType(TypeOfAny.special_form))

        # TODO(jukka): We could return as soon as we find an error if messages is None.

        # Collect dict of all actual arguments matched to formal arguments, with occurrence count
        all_actuals: dict[int, int] = {}
        for actuals in formal_to_actual:
            for a in actuals:
                all_actuals[a] = all_actuals.get(a, 0) + 1

        ok, is_unexpected_arg_error = self.check_for_extra_actual_arguments(
            callee, actual_types, actual_kinds, actual_names, all_actuals, context
        )

        # Check for too many or few values for formals.
        for i, kind in enumerate(callee.arg_kinds):
            mapped_args = formal_to_actual[i]
            if kind.is_required() and not mapped_args and not is_unexpected_arg_error:
                # No actual for a mandatory formal
                if kind.is_positional():
                    self.msg.too_few_arguments(callee, context, actual_names)
                    if object_type and callable_name and "." in callable_name:
                        self.missing_classvar_callable_note(object_type, callable_name, context)
                else:
                    argname = callee.arg_names[i] or "?"
                    self.msg.missing_named_argument(callee, context, argname)
                ok = False
            elif not kind.is_star() and is_duplicate_mapping(
                mapped_args, actual_types, actual_kinds
            ):
                if self.chk.in_checked_function() or isinstance(
                    get_proper_type(actual_types[mapped_args[0]]), TupleType
                ):
                    self.msg.duplicate_argument_value(callee, i, context)
                    ok = False
            elif (
                kind.is_named()
                and mapped_args
                and actual_kinds[mapped_args[0]] not in [nodes.ARG_NAMED, nodes.ARG_STAR2]
            ):
                # Positional argument when expecting a keyword argument.
                self.msg.too_many_positional_arguments(callee, context)
                ok = False
            elif callee.param_spec() is not None:
                if not mapped_args and callee.special_sig != "partial":
                    self.msg.too_few_arguments(callee, context, actual_names)
                    ok = False
                elif len(mapped_args) > 1:
                    paramspec_entries = sum(
                        isinstance(get_proper_type(actual_types[k]), ParamSpecType)
                        for k in mapped_args
                    )
                    if actual_kinds[mapped_args[0]] == nodes.ARG_STAR and paramspec_entries > 1:
                        self.msg.fail("ParamSpec.args should only be passed once", context)
                        ok = False
                    if actual_kinds[mapped_args[0]] == nodes.ARG_STAR2 and paramspec_entries > 1:
                        self.msg.fail("ParamSpec.kwargs should only be passed once", context)
                        ok = False
        return ok

    def check_for_extra_actual_arguments(
        self,
        callee: CallableType,
        actual_types: list[Type],
        actual_kinds: list[ArgKind],
        actual_names: Sequence[str | None] | None,
        all_actuals: dict[int, int],
        context: Context,
    ) -> tuple[bool, bool]:
        """Check for extra actual arguments.

        Return tuple (was everything ok,
                      was there an extra keyword argument error [used to avoid duplicate errors]).
        """

        is_unexpected_arg_error = False  # Keep track of errors to avoid duplicate errors
        ok = True  # False if we've found any error

        for i, kind in enumerate(actual_kinds):
            if (
                i not in all_actuals
                and
                # We accept the other iterables than tuple (including Any)
                # as star arguments because they could be empty, resulting no arguments.
                (kind != nodes.ARG_STAR or is_non_empty_tuple(actual_types[i]))
                and
                # Accept all types for double-starred arguments, because they could be empty
                # dictionaries and we can't tell it from their types
                kind != nodes.ARG_STAR2
            ):
                # Extra actual: not matched by a formal argument.
                ok = False
                if kind != nodes.ARG_NAMED:
                    self.msg.too_many_arguments(callee, context)
                else:
                    assert actual_names, "Internal error: named kinds without names given"
                    act_name = actual_names[i]
                    assert act_name is not None
                    act_type = actual_types[i]
                    self.msg.unexpected_keyword_argument(callee, act_name, act_type, context)
                    is_unexpected_arg_error = True
            elif (
                kind == nodes.ARG_STAR and nodes.ARG_STAR not in callee.arg_kinds
            ) or kind == nodes.ARG_STAR2:
                actual_type = get_proper_type(actual_types[i])
                if isinstance(actual_type, (TupleType, TypedDictType)):
                    if all_actuals.get(i, 0) < len(actual_type.items):
                        # Too many tuple/dict items as some did not match.
                        if kind != nodes.ARG_STAR2 or not isinstance(actual_type, TypedDictType):
                            self.msg.too_many_arguments(callee, context)
                        else:
                            self.msg.too_many_arguments_from_typed_dict(
                                callee, actual_type, context
                            )
                            is_unexpected_arg_error = True
                        ok = False
                # *args/**kwargs can be applied even if the function takes a fixed
                # number of positional arguments. This may succeed at runtime.

        return ok, is_unexpected_arg_error

    def missing_classvar_callable_note(
        self, object_type: Type, callable_name: str, context: Context
    ) -> None:
        if isinstance(object_type, ProperType) and isinstance(object_type, Instance):
            _, var_name = callable_name.rsplit(".", maxsplit=1)
            node = object_type.type.get(var_name)
            if node is not None and isinstance(node.node, Var):
                if not node.node.is_inferred and not node.node.is_classvar:
                    self.msg.note(
                        f'"{var_name}" is considered instance variable,'
                        " to make it class variable use ClassVar[...]",
                        context,
                    )

    def check_argument_types(
        self,
        arg_types: list[Type],
        arg_kinds: list[ArgKind],
        args: list[Expression],
        callee: CallableType,
        formal_to_actual: list[list[int]],
        context: Context,
        check_arg: ArgChecker | None = None,
        object_type: Type | None = None,
    ) -> None:
        """Check argument types against a callable type.

        Report errors if the argument types are not compatible.

        The check_call docstring describes some of the arguments.
        """
        check_arg = check_arg or self.check_arg
        # Keep track of consumed tuple *arg items.
        mapper = ArgTypeExpander(self.argument_infer_context())

        for arg_type, arg_kind in zip(arg_types, arg_kinds):
            arg_type = get_proper_type(arg_type)
            if arg_kind == nodes.ARG_STAR and not self.is_valid_var_arg(arg_type):
                self.msg.invalid_var_arg(arg_type, context)
            if arg_kind == nodes.ARG_STAR2 and not self.is_valid_keyword_var_arg(arg_type):
                is_mapping = is_subtype(
                    arg_type, self.chk.named_type("_typeshed.SupportsKeysAndGetItem")
                )
                self.msg.invalid_keyword_var_arg(arg_type, is_mapping, context)

        for i, actuals in enumerate(formal_to_actual):
            orig_callee_arg_type = get_proper_type(callee.arg_types[i])

            # Checking the case that we have more than one item but the first argument
            # is an unpack, so this would be something like:
            # [Tuple[Unpack[Ts]], int]
            #
            # In this case we have to check everything together, we do this by re-unifying
            # the suffices to the tuple, e.g. a single actual like
            # Tuple[Unpack[Ts], int]
            expanded_tuple = False
            actual_kinds = [arg_kinds[a] for a in actuals]
            if len(actuals) > 1:
                p_actual_type = get_proper_type(arg_types[actuals[0]])
                if (
                    isinstance(p_actual_type, TupleType)
                    and len(p_actual_type.items) == 1
                    and isinstance(p_actual_type.items[0], UnpackType)
                    and actual_kinds == [nodes.ARG_STAR] + [nodes.ARG_POS] * (len(actuals) - 1)
                ):
                    actual_types = [p_actual_type.items[0]] + [arg_types[a] for a in actuals[1:]]
                    if isinstance(orig_callee_arg_type, UnpackType):
                        p_callee_type = get_proper_type(orig_callee_arg_type.type)
                        if isinstance(p_callee_type, TupleType):
                            assert p_callee_type.items
                            callee_arg_types = p_callee_type.items
                            callee_arg_kinds = [nodes.ARG_STAR] + [nodes.ARG_POS] * (
                                len(p_callee_type.items) - 1
                            )
                            expanded_tuple = True

            if not expanded_tuple:
                actual_types = [arg_types[a] for a in actuals]
                if isinstance(orig_callee_arg_type, UnpackType):
                    unpacked_type = get_proper_type(orig_callee_arg_type.type)
                    if isinstance(unpacked_type, TupleType):
                        inner_unpack_index = find_unpack_in_list(unpacked_type.items)
                        if inner_unpack_index is None:
                            callee_arg_types = unpacked_type.items
                            callee_arg_kinds = [ARG_POS] * len(actuals)
                        else:
                            inner_unpack = unpacked_type.items[inner_unpack_index]
                            assert isinstance(inner_unpack, UnpackType)
                            inner_unpacked_type = get_proper_type(inner_unpack.type)
                            if isinstance(inner_unpacked_type, TypeVarTupleType):
                                # This branch mimics the expanded_tuple case above but for
                                # the case where caller passed a single * unpacked tuple argument.
                                callee_arg_types = unpacked_type.items
                                callee_arg_kinds = [
                                    ARG_POS if i != inner_unpack_index else ARG_STAR
                                    for i in range(len(unpacked_type.items))
                                ]
                            else:
                                # We assume heterogeneous tuples are desugared earlier.
                                assert isinstance(inner_unpacked_type, Instance)
                                assert inner_unpacked_type.type.fullname == "builtins.tuple"
                                callee_arg_types = (
                                    unpacked_type.items[:inner_unpack_index]
                                    + [inner_unpacked_type.args[0]]
                                    * (len(actuals) - len(unpacked_type.items) + 1)
                                    + unpacked_type.items[inner_unpack_index + 1 :]
                                )
                                callee_arg_kinds = [ARG_POS] * len(actuals)
                    elif isinstance(unpacked_type, TypeVarTupleType):
                        callee_arg_types = [orig_callee_arg_type]
                        callee_arg_kinds = [ARG_STAR]
                    else:
                        assert isinstance(unpacked_type, Instance)
                        assert unpacked_type.type.fullname == "builtins.tuple"
                        callee_arg_types = [unpacked_type.args[0]] * len(actuals)
                        callee_arg_kinds = [ARG_POS] * len(actuals)
                else:
                    callee_arg_types = [orig_callee_arg_type] * len(actuals)
                    callee_arg_kinds = [callee.arg_kinds[i]] * len(actuals)

            assert len(actual_types) == len(actuals) == len(actual_kinds)

            if len(callee_arg_types) != len(actual_types):
                if len(actual_types) > len(callee_arg_types):
                    self.chk.msg.too_many_arguments(callee, context)
                else:
                    self.chk.msg.too_few_arguments(callee, context, None)
                continue

            assert len(callee_arg_types) == len(actual_types)
            assert len(callee_arg_types) == len(callee_arg_kinds)
            for actual, actual_type, actual_kind, callee_arg_type, callee_arg_kind in zip(
                actuals, actual_types, actual_kinds, callee_arg_types, callee_arg_kinds
            ):
                # Check that a *arg is valid as varargs.
                expanded_actual = mapper.expand_actual_type(
                    actual_type,
                    actual_kind,
                    callee.arg_names[i],
                    callee_arg_kind,
                    allow_unpack=isinstance(callee_arg_type, UnpackType),
                )
                check_arg(
                    expanded_actual,
                    actual_type,
                    actual_kind,
                    callee_arg_type,
                    actual + 1,
                    i + 1,
                    callee,
                    object_type,
                    args[actual],
                    context,
                )

    def check_arg(
        self,
        caller_type: Type,
        original_caller_type: Type,
        caller_kind: ArgKind,
        callee_type: Type,
        n: int,
        m: int,
        callee: CallableType,
        object_type: Type | None,
        context: Context,
        outer_context: Context,
    ) -> None:
        """Check the type of a single argument in a call."""
        caller_type = get_proper_type(caller_type)
        original_caller_type = get_proper_type(original_caller_type)
        callee_type = get_proper_type(callee_type)

        if isinstance(caller_type, DeletedType):
            self.msg.deleted_as_rvalue(caller_type, context)
        # Only non-abstract non-protocol class can be given where Type[...] is expected...
        elif self.has_abstract_type_part(caller_type, callee_type):
            self.msg.concrete_only_call(callee_type, context)
        elif not is_subtype(caller_type, callee_type, options=self.chk.options):
            error = self.msg.incompatible_argument(
                n,
                m,
                callee,
                original_caller_type,
                caller_kind,
                object_type=object_type,
                context=context,
                outer_context=outer_context,
            )
            if not caller_kind.is_star():
                # For *args and **kwargs this note would be incorrect - we're comparing
                # iterable/mapping type with union of relevant arg types.
                self.msg.incompatible_argument_note(
                    original_caller_type, callee_type, context, parent_error=error
                )
            if not self.msg.prefer_simple_messages():
                self.chk.check_possible_missing_await(
                    caller_type, callee_type, context, error.code
                )

    def check_overload_call(
        self,
        callee: Overloaded,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        callable_name: str | None,
        object_type: Type | None,
        context: Context,
    ) -> tuple[Type, Type]:
        """Checks a call to an overloaded function."""
        # Normalize unpacked kwargs before checking the call.
        callee = callee.with_unpacked_kwargs()
        arg_types = self.infer_arg_types_in_empty_context(args)
        # Step 1: Filter call targets to remove ones where the argument counts don't match
        plausible_targets = self.plausible_overload_call_targets(
            arg_types, arg_kinds, arg_names, callee
        )

        # Step 2: If the arguments contain a union, we try performing union math first,
        #         instead of picking the first matching overload.
        #         This is because picking the first overload often ends up being too greedy:
        #         for example, when we have a fallback alternative that accepts an unrestricted
        #         typevar. See https://github.com/python/mypy/issues/4063 for related discussion.
        erased_targets: list[CallableType] | None = None
        unioned_result: tuple[Type, Type] | None = None

        # Determine whether we need to encourage union math. This should be generally safe,
        # as union math infers better results in the vast majority of cases, but it is very
        # computationally intensive.
        none_type_var_overlap = self.possible_none_type_var_overlap(arg_types, plausible_targets)
        union_interrupted = False  # did we try all union combinations?
        if any(self.real_union(arg) for arg in arg_types):
            try:
                with self.msg.filter_errors():
                    unioned_return = self.union_overload_result(
                        plausible_targets,
                        args,
                        arg_types,
                        arg_kinds,
                        arg_names,
                        callable_name,
                        object_type,
                        none_type_var_overlap,
                        context,
                    )
            except TooManyUnions:
                union_interrupted = True
            else:
                # Record if we succeeded. Next we need to see if maybe normal procedure
                # gives a narrower type.
                if unioned_return:
                    returns, inferred_types = zip(*unioned_return)
                    # Note that we use `combine_function_signatures` instead of just returning
                    # a union of inferred callables because for example a call
                    # Union[int -> int, str -> str](Union[int, str]) is invalid and
                    # we don't want to introduce internal inconsistencies.
                    unioned_result = (
                        make_simplified_union(list(returns), context.line, context.column),
                        self.combine_function_signatures(get_proper_types(inferred_types)),
                    )

        # Step 3: We try checking each branch one-by-one.
        inferred_result = self.infer_overload_return_type(
            plausible_targets,
            args,
            arg_types,
            arg_kinds,
            arg_names,
            callable_name,
            object_type,
            context,
        )
        # If any of checks succeed, stop early.
        if inferred_result is not None and unioned_result is not None:
            # Both unioned and direct checks succeeded, choose the more precise type.
            if (
                is_subtype(inferred_result[0], unioned_result[0])
                and not isinstance(get_proper_type(inferred_result[0]), AnyType)
                and not none_type_var_overlap
            ):
                return inferred_result
            return unioned_result
        elif unioned_result is not None:
            return unioned_result
        elif inferred_result is not None:
            return inferred_result

        # Step 4: Failure. At this point, we know there is no match. We fall back to trying
        #         to find a somewhat plausible overload target using the erased types
        #         so we can produce a nice error message.
        #
        #         For example, suppose the user passes a value of type 'List[str]' into an
        #         overload with signatures f(x: int) -> int and f(x: List[int]) -> List[int].
        #
        #         Neither alternative matches, but we can guess the user probably wants the
        #         second one.
        erased_targets = self.overload_erased_call_targets(
            plausible_targets, arg_types, arg_kinds, arg_names, args, context
        )

        # Step 5: We try and infer a second-best alternative if possible. If not, fall back
        #         to using 'Any'.
        if len(erased_targets) > 0:
            # Pick the first plausible erased target as the fallback
            # TODO: Adjust the error message here to make it clear there was no match.
            #       In order to do this, we need to find a clean way of associating
            #       a note with whatever error message 'self.check_call' will generate.
            #       In particular, the note's line and column numbers need to be the same
            #       as the error's.
            target: Type = erased_targets[0]
        else:
            # There was no plausible match: give up
            target = AnyType(TypeOfAny.from_error)
            if not is_operator_method(callable_name):
                code = None
            else:
                code = codes.OPERATOR
            self.msg.no_variant_matches_arguments(callee, arg_types, context, code=code)

        result = self.check_call(
            target,
            args,
            arg_kinds,
            context,
            arg_names,
            callable_name=callable_name,
            object_type=object_type,
        )
        # Do not show the extra error if the union math was forced.
        if union_interrupted and not none_type_var_overlap:
            self.chk.fail(message_registry.TOO_MANY_UNION_COMBINATIONS, context)
        return result

    def plausible_overload_call_targets(
        self,
        arg_types: list[Type],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        overload: Overloaded,
    ) -> list[CallableType]:
        """Returns all overload call targets that having matching argument counts.

        If the given args contains a star-arg (*arg or **kwarg argument, except for
        ParamSpec), this method will ensure all star-arg overloads appear at the start
        of the list, instead of their usual location.

        The only exception is if the starred argument is something like a Tuple or a
        NamedTuple, which has a definitive "shape". If so, we don't move the corresponding
        alternative to the front since we can infer a more precise match using the original
        order."""

        def has_shape(typ: Type) -> bool:
            typ = get_proper_type(typ)
            return isinstance(typ, (TupleType, TypedDictType)) or (
                isinstance(typ, Instance) and typ.type.is_named_tuple
            )

        matches: list[CallableType] = []
        star_matches: list[CallableType] = []

        args_have_var_arg = False
        args_have_kw_arg = False
        for kind, typ in zip(arg_kinds, arg_types):
            if kind == ARG_STAR and not has_shape(typ):
                args_have_var_arg = True
            if kind == ARG_STAR2 and not has_shape(typ):
                args_have_kw_arg = True

        for typ in overload.items:
            formal_to_actual = map_actuals_to_formals(
                arg_kinds, arg_names, typ.arg_kinds, typ.arg_names, lambda i: arg_types[i]
            )
            with self.msg.filter_errors():
                if typ.param_spec() is not None:
                    # ParamSpec can be expanded in a lot of different ways. We may try
                    # to expand it here instead, but picking an impossible overload
                    # is safe: it will be filtered out later.
                    # Unlike other var-args signatures, ParamSpec produces essentially
                    # a fixed signature, so there's no need to push them to the top.
                    matches.append(typ)
                elif self.check_argument_count(
                    typ, arg_types, arg_kinds, arg_names, formal_to_actual, None
                ):
                    if args_have_var_arg and typ.is_var_arg:
                        star_matches.append(typ)
                    elif args_have_kw_arg and typ.is_kw_arg:
                        star_matches.append(typ)
                    else:
                        matches.append(typ)

        return star_matches + matches

    def infer_overload_return_type(
        self,
        plausible_targets: list[CallableType],
        args: list[Expression],
        arg_types: list[Type],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        callable_name: str | None,
        object_type: Type | None,
        context: Context,
    ) -> tuple[Type, Type] | None:
        """Attempts to find the first matching callable from the given list.

        If a match is found, returns a tuple containing the result type and the inferred
        callee type. (This tuple is meant to be eventually returned by check_call.)
        If multiple targets match due to ambiguous Any parameters, returns (AnyType, AnyType).
        If no targets match, returns None.

        Assumes all of the given targets have argument counts compatible with the caller.
        """

        matches: list[CallableType] = []
        return_types: list[Type] = []
        inferred_types: list[Type] = []
        args_contain_any = any(map(has_any_type, arg_types))
        type_maps: list[dict[Expression, Type]] = []

        for typ in plausible_targets:
            assert self.msg is self.chk.msg
            with self.msg.filter_errors() as w:
                with self.chk.local_type_map() as m:
                    ret_type, infer_type = self.check_call(
                        callee=typ,
                        args=args,
                        arg_kinds=arg_kinds,
                        arg_names=arg_names,
                        context=context,
                        callable_name=callable_name,
                        object_type=object_type,
                    )
            is_match = not w.has_new_errors()
            if is_match:
                # Return early if possible; otherwise record info, so we can
                # check for ambiguity due to 'Any' below.
                if not args_contain_any:
                    self.chk.store_types(m)
                    return ret_type, infer_type
                p_infer_type = get_proper_type(infer_type)
                if isinstance(p_infer_type, CallableType):
                    # Prefer inferred types if possible, this will avoid false triggers for
                    # Any-ambiguity caused by arguments with Any passed to generic overloads.
                    matches.append(p_infer_type)
                else:
                    matches.append(typ)
                return_types.append(ret_type)
                inferred_types.append(infer_type)
                type_maps.append(m)

        if not matches:
            return None
        elif any_causes_overload_ambiguity(matches, return_types, arg_types, arg_kinds, arg_names):
            # An argument of type or containing the type 'Any' caused ambiguity.
            # We try returning a precise type if we can. If not, we give up and just return 'Any'.
            if all_same_types(return_types):
                self.chk.store_types(type_maps[0])
                return return_types[0], inferred_types[0]
            elif all_same_types([erase_type(typ) for typ in return_types]):
                self.chk.store_types(type_maps[0])
                return erase_type(return_types[0]), erase_type(inferred_types[0])
            else:
                return self.check_call(
                    callee=AnyType(TypeOfAny.special_form),
                    args=args,
                    arg_kinds=arg_kinds,
                    arg_names=arg_names,
                    context=context,
                    callable_name=callable_name,
                    object_type=object_type,
                )
        else:
            # Success! No ambiguity; return the first match.
            self.chk.store_types(type_maps[0])
            return return_types[0], inferred_types[0]

    def overload_erased_call_targets(
        self,
        plausible_targets: list[CallableType],
        arg_types: list[Type],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        args: list[Expression],
        context: Context,
    ) -> list[CallableType]:
        """Returns a list of all targets that match the caller after erasing types.

        Assumes all of the given targets have argument counts compatible with the caller.
        """
        matches: list[CallableType] = []
        for typ in plausible_targets:
            if self.erased_signature_similarity(
                arg_types, arg_kinds, arg_names, args, typ, context
            ):
                matches.append(typ)
        return matches

    def possible_none_type_var_overlap(
        self, arg_types: list[Type], plausible_targets: list[CallableType]
    ) -> bool:
        """Heuristic to determine whether we need to try forcing union math.

        This is needed to avoid greedy type variable match in situations like this:
            @overload
            def foo(x: None) -> None: ...
            @overload
            def foo(x: T) -> list[T]: ...

            x: int | None
            foo(x)
        we want this call to infer list[int] | None, not list[int | None].
        """
        if not plausible_targets or not arg_types:
            return False
        has_optional_arg = False
        for arg_type in get_proper_types(arg_types):
            if not isinstance(arg_type, UnionType):
                continue
            for item in get_proper_types(arg_type.items):
                if isinstance(item, NoneType):
                    has_optional_arg = True
                    break
        if not has_optional_arg:
            return False

        min_prefix = min(len(c.arg_types) for c in plausible_targets)
        for i in range(min_prefix):
            if any(
                isinstance(get_proper_type(c.arg_types[i]), NoneType) for c in plausible_targets
            ) and any(
                isinstance(get_proper_type(c.arg_types[i]), TypeVarType) for c in plausible_targets
            ):
                return True
        return False

    def union_overload_result(
        self,
        plausible_targets: list[CallableType],
        args: list[Expression],
        arg_types: list[Type],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        callable_name: str | None,
        object_type: Type | None,
        none_type_var_overlap: bool,
        context: Context,
        level: int = 0,
    ) -> list[tuple[Type, Type]] | None:
        """Accepts a list of overload signatures and attempts to match calls by destructuring
        the first union.

        Return a list of (<return type>, <inferred variant type>) if call succeeds for every
        item of the desctructured union. Returns None if there is no match.
        """
        # Step 1: If we are already too deep, then stop immediately. Otherwise mypy might
        # hang for long time because of a weird overload call. The caller will get
        # the exception and generate an appropriate note message, if needed.
        if level >= MAX_UNIONS:
            raise TooManyUnions

        # Step 2: Find position of the first union in arguments. Return the normal inferred
        # type if no more unions left.
        for idx, typ in enumerate(arg_types):
            if self.real_union(typ):
                break
        else:
            # No unions in args, just fall back to normal inference
            with self.type_overrides_set(args, arg_types):
                res = self.infer_overload_return_type(
                    plausible_targets,
                    args,
                    arg_types,
                    arg_kinds,
                    arg_names,
                    callable_name,
                    object_type,
                    context,
                )
            if res is not None:
                return [res]
            return None

        # Step 3: Try a direct match before splitting to avoid unnecessary union splits
        # and save performance.
        if not none_type_var_overlap:
            with self.type_overrides_set(args, arg_types):
                direct = self.infer_overload_return_type(
                    plausible_targets,
                    args,
                    arg_types,
                    arg_kinds,
                    arg_names,
                    callable_name,
                    object_type,
                    context,
                )
            if direct is not None and not isinstance(
                get_proper_type(direct[0]), (UnionType, AnyType)
            ):
                # We only return non-unions soon, to avoid greedy match.
                return [direct]

        # Step 4: Split the first remaining union type in arguments into items and
        # try to match each item individually (recursive).
        first_union = get_proper_type(arg_types[idx])
        assert isinstance(first_union, UnionType)
        res_items = []
        for item in first_union.relevant_items():
            new_arg_types = arg_types.copy()
            new_arg_types[idx] = item
            sub_result = self.union_overload_result(
                plausible_targets,
                args,
                new_arg_types,
                arg_kinds,
                arg_names,
                callable_name,
                object_type,
                none_type_var_overlap,
                context,
                level + 1,
            )
            if sub_result is not None:
                res_items.extend(sub_result)
            else:
                # Some item doesn't match, return soon.
                return None

        # Step 5: If splitting succeeded, then filter out duplicate items before returning.
        seen: set[tuple[Type, Type]] = set()
        result = []
        for pair in res_items:
            if pair not in seen:
                seen.add(pair)
                result.append(pair)
        return result

    def real_union(self, typ: Type) -> bool:
        typ = get_proper_type(typ)
        return isinstance(typ, UnionType) and len(typ.relevant_items()) > 1

    @contextmanager
    def type_overrides_set(
        self, exprs: Sequence[Expression], overrides: Sequence[Type]
    ) -> Iterator[None]:
        """Set _temporary_ type overrides for given expressions."""
        assert len(exprs) == len(overrides)
        for expr, typ in zip(exprs, overrides):
            self.type_overrides[expr] = typ
        try:
            yield
        finally:
            for expr in exprs:
                del self.type_overrides[expr]

    def combine_function_signatures(self, types: list[ProperType]) -> AnyType | CallableType:
        """Accepts a list of function signatures and attempts to combine them together into a
        new CallableType consisting of the union of all of the given arguments and return types.

        If there is at least one non-callable type, return Any (this can happen if there is
        an ambiguity because of Any in arguments).
        """
        assert types, "Trying to merge no callables"
        if not all(isinstance(c, CallableType) for c in types):
            return AnyType(TypeOfAny.special_form)
        callables = cast("list[CallableType]", types)
        if len(callables) == 1:
            return callables[0]

        # Note: we are assuming here that if a user uses some TypeVar 'T' in
        # two different functions, they meant for that TypeVar to mean the
        # same thing.
        #
        # This function will make sure that all instances of that TypeVar 'T'
        # refer to the same underlying TypeVarType objects to simplify the union-ing
        # logic below.
        #
        # (If the user did *not* mean for 'T' to be consistently bound to the
        # same type in their overloads, well, their code is probably too
        # confusing and ought to be re-written anyways.)
        callables, variables = merge_typevars_in_callables_by_name(callables)

        new_args: list[list[Type]] = [[] for _ in range(len(callables[0].arg_types))]
        new_kinds = list(callables[0].arg_kinds)
        new_returns: list[Type] = []

        too_complex = False
        for target in callables:
            # We fall back to Callable[..., Union[<returns>]] if the functions do not have
            # the exact same signature. The only exception is if one arg is optional and
            # the other is positional: in that case, we continue unioning (and expect a
            # positional arg).
            # TODO: Enhance the merging logic to handle a wider variety of signatures.
            if len(new_kinds) != len(target.arg_kinds):
                too_complex = True
                break
            for i, (new_kind, target_kind) in enumerate(zip(new_kinds, target.arg_kinds)):
                if new_kind == target_kind:
                    continue
                elif new_kind.is_positional() and target_kind.is_positional():
                    new_kinds[i] = ARG_POS
                else:
                    too_complex = True
                    break

            if too_complex:
                break  # outer loop

            for i, arg in enumerate(target.arg_types):
                new_args[i].append(arg)
            new_returns.append(target.ret_type)

        union_return = make_simplified_union(new_returns)
        if too_complex:
            any = AnyType(TypeOfAny.special_form)
            return callables[0].copy_modified(
                arg_types=[any, any],
                arg_kinds=[ARG_STAR, ARG_STAR2],
                arg_names=[None, None],
                ret_type=union_return,
                variables=variables,
                implicit=True,
            )

        final_args = []
        for args_list in new_args:
            new_type = make_simplified_union(args_list)
            final_args.append(new_type)

        return callables[0].copy_modified(
            arg_types=final_args,
            arg_kinds=new_kinds,
            ret_type=union_return,
            variables=variables,
            implicit=True,
        )

    def erased_signature_similarity(
        self,
        arg_types: list[Type],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        args: list[Expression],
        callee: CallableType,
        context: Context,
    ) -> bool:
        """Determine whether arguments could match the signature at runtime, after
        erasing types."""
        formal_to_actual = map_actuals_to_formals(
            arg_kinds, arg_names, callee.arg_kinds, callee.arg_names, lambda i: arg_types[i]
        )

        with self.msg.filter_errors():
            if not self.check_argument_count(
                callee, arg_types, arg_kinds, arg_names, formal_to_actual, None
            ):
                # Too few or many arguments -> no match.
                return False

        def check_arg(
            caller_type: Type,
            original_ccaller_type: Type,
            caller_kind: ArgKind,
            callee_type: Type,
            n: int,
            m: int,
            callee: CallableType,
            object_type: Type | None,
            context: Context,
            outer_context: Context,
        ) -> None:
            if not arg_approximate_similarity(caller_type, callee_type):
                # No match -- exit early since none of the remaining work can change
                # the result.
                raise Finished

        try:
            self.check_argument_types(
                arg_types,
                arg_kinds,
                args,
                callee,
                formal_to_actual,
                context=context,
                check_arg=check_arg,
            )
            return True
        except Finished:
            return False

    def apply_generic_arguments(
        self,
        callable: CallableType,
        types: Sequence[Type | None],
        context: Context,
        skip_unsatisfied: bool = False,
    ) -> CallableType:
        """Simple wrapper around mypy.applytype.apply_generic_arguments."""
        return applytype.apply_generic_arguments(
            callable,
            types,
            self.msg.incompatible_typevar_value,
            context,
            skip_unsatisfied=skip_unsatisfied,
        )

    def check_any_type_call(self, args: list[Expression], callee: Type) -> tuple[Type, Type]:
        self.infer_arg_types_in_empty_context(args)
        callee = get_proper_type(callee)
        if isinstance(callee, AnyType):
            return (
                AnyType(TypeOfAny.from_another_any, source_any=callee),
                AnyType(TypeOfAny.from_another_any, source_any=callee),
            )
        else:
            return AnyType(TypeOfAny.special_form), AnyType(TypeOfAny.special_form)

    def check_union_call(
        self,
        callee: UnionType,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None] | None,
        context: Context,
    ) -> tuple[Type, Type]:
        with self.msg.disable_type_names():
            results = [
                self.check_call(subtype, args, arg_kinds, context, arg_names)
                for subtype in callee.relevant_items()
            ]

        return (make_simplified_union([res[0] for res in results]), callee)

    def visit_member_expr(self, e: MemberExpr, is_lvalue: bool = False) -> Type:
        """Visit member expression (of form e.id)."""
        self.chk.module_refs.update(extract_refexpr_names(e))
        result = self.analyze_ordinary_member_access(e, is_lvalue)
        narrowed = self.narrow_type_from_binder(e, result)
        self.chk.warn_deprecated(e.node, e)
        return narrowed

    def analyze_ordinary_member_access(
        self, e: MemberExpr, is_lvalue: bool, rvalue: Expression | None = None
    ) -> Type:
        """Analyse member expression or member lvalue.

        An rvalue can be provided optionally to infer better setter type when is_lvalue is True.
        """
        if e.kind is not None:
            # This is a reference to a module attribute.
            return self.analyze_ref_expr(e)
        else:
            # This is a reference to a non-module attribute.
            original_type = self.accept(e.expr, is_callee=self.is_callee)
            base = e.expr
            module_symbol_table = None

            if isinstance(base, RefExpr) and isinstance(base.node, MypyFile):
                module_symbol_table = base.node.names
            if isinstance(base, RefExpr) and isinstance(base.node, Var):
                # This is needed to special case self-types, so we don't need to track
                # these flags separately in checkmember.py.
                is_self = base.node.is_self or base.node.is_cls
            else:
                is_self = False

            member_type = analyze_member_access(
                e.name,
                original_type,
                e,
                is_lvalue=is_lvalue,
                is_super=False,
                is_operator=False,
                original_type=original_type,
                chk=self.chk,
                in_literal_context=self.is_literal_context(),
                module_symbol_table=module_symbol_table,
                is_self=is_self,
                rvalue=rvalue,
            )

            return member_type

    def analyze_external_member_access(
        self, member: str, base_type: Type, context: Context
    ) -> Type:
        """Analyse member access that is external, i.e. it cannot
        refer to private definitions. Return the result type.
        """
        # TODO remove; no private definitions in mypy
        return analyze_member_access(
            member,
            base_type,
            context,
            is_lvalue=False,
            is_super=False,
            is_operator=False,
            original_type=base_type,
            chk=self.chk,
            in_literal_context=self.is_literal_context(),
        )

    def is_literal_context(self) -> bool:
        return is_literal_type_like(self.type_context[-1])

    def infer_literal_expr_type(self, value: LiteralValue, fallback_name: str) -> Type:
        """Analyzes the given literal expression and determines if we should be
        inferring an Instance type, a Literal[...] type, or an Instance that
        remembers the original literal. We...

        1. ...Infer a normal Instance in most circumstances.

        2. ...Infer a Literal[...] if we're in a literal context. For example, if we
           were analyzing the "3" in "foo(3)" where "foo" has a signature of
           "def foo(Literal[3]) -> None", we'd want to infer that the "3" has a
           type of Literal[3] instead of Instance.

        3. ...Infer an Instance that remembers the original Literal if we're declaring
           a Final variable with an inferred type -- for example, "bar" in "bar: Final = 3"
           would be assigned an Instance that remembers it originated from a '3'. See
           the comments in Instance's constructor for more details.
        """
        typ = self.named_type(fallback_name)
        if self.is_literal_context():
            return LiteralType(value=value, fallback=typ)
        else:
            return typ.copy_modified(
                last_known_value=LiteralType(
                    value=value, fallback=typ, line=typ.line, column=typ.column
                )
            )

    def concat_tuples(self, left: TupleType, right: TupleType) -> TupleType:
        """Concatenate two fixed length tuples."""
        assert not (find_unpack_in_list(left.items) and find_unpack_in_list(right.items))
        return TupleType(
            items=left.items + right.items, fallback=self.named_type("builtins.tuple")
        )

    def visit_int_expr(self, e: IntExpr) -> Type:
        """Type check an integer literal (trivial)."""
        return self.infer_literal_expr_type(e.value, "builtins.int")

    def visit_str_expr(self, e: StrExpr) -> Type:
        """Type check a string literal (trivial)."""
        return self.infer_literal_expr_type(e.value, "builtins.str")

    def visit_bytes_expr(self, e: BytesExpr) -> Type:
        """Type check a bytes literal (trivial)."""
        return self.infer_literal_expr_type(e.value, "builtins.bytes")

    def visit_float_expr(self, e: FloatExpr) -> Type:
        """Type check a float literal (trivial)."""
        return self.named_type("builtins.float")

    def visit_complex_expr(self, e: ComplexExpr) -> Type:
        """Type check a complex literal."""
        return self.named_type("builtins.complex")

    def visit_ellipsis(self, e: EllipsisExpr) -> Type:
        """Type check '...'."""
        return self.named_type("builtins.ellipsis")

    def visit_op_expr(self, e: OpExpr) -> Type:
        """Type check a binary operator expression."""
        if e.analyzed:
            # It's actually a type expression X | Y.
            return self.accept(e.analyzed)
        if e.op == "and" or e.op == "or":
            return self.check_boolean_op(e)
        if e.op == "*" and isinstance(e.left, ListExpr):
            # Expressions of form [...] * e get special type inference.
            return self.check_list_multiply(e)
        if e.op == "%":
            if isinstance(e.left, BytesExpr):
                return self.strfrm_checker.check_str_interpolation(e.left, e.right)
            if isinstance(e.left, StrExpr):
                return self.strfrm_checker.check_str_interpolation(e.left, e.right)
        left_type = self.accept(e.left)

        proper_left_type = get_proper_type(left_type)
        if isinstance(proper_left_type, TupleType) and e.op == "+":
            left_add_method = proper_left_type.partial_fallback.type.get("__add__")
            if left_add_method and left_add_method.fullname == "builtins.tuple.__add__":
                proper_right_type = get_proper_type(self.accept(e.right))
                if isinstance(proper_right_type, TupleType):
                    right_radd_method = proper_right_type.partial_fallback.type.get("__radd__")
                    if right_radd_method is None:
                        # One cannot have two variadic items in the same tuple.
                        if (
                            find_unpack_in_list(proper_left_type.items) is None
                            or find_unpack_in_list(proper_right_type.items) is None
                        ):
                            return self.concat_tuples(proper_left_type, proper_right_type)
                elif (
                    PRECISE_TUPLE_TYPES in self.chk.options.enable_incomplete_feature
                    and isinstance(proper_right_type, Instance)
                    and self.chk.type_is_iterable(proper_right_type)
                ):
                    # Handle tuple[X, Y] + tuple[Z, ...] = tuple[X, Y, *tuple[Z, ...]].
                    right_radd_method = proper_right_type.type.get("__radd__")
                    if (
                        right_radd_method is None
                        and proper_left_type.partial_fallback.type.fullname == "builtins.tuple"
                        and find_unpack_in_list(proper_left_type.items) is None
                    ):
                        item_type = self.chk.iterable_item_type(proper_right_type, e)
                        mapped = self.chk.named_generic_type("builtins.tuple", [item_type])
                        return proper_left_type.copy_modified(
                            items=proper_left_type.items + [UnpackType(mapped)]
                        )

        use_reverse: UseReverse = USE_REVERSE_DEFAULT
        if e.op == "|":
            if is_named_instance(proper_left_type, "builtins.dict"):
                # This is a special case for `dict | TypedDict`.
                # 1. Find `dict | TypedDict` case
                # 2. Switch `dict.__or__` to `TypedDict.__ror__` (the same from both runtime and typing perspective)
                proper_right_type = get_proper_type(self.accept(e.right))
                if isinstance(proper_right_type, TypedDictType):
                    use_reverse = USE_REVERSE_ALWAYS
            if isinstance(proper_left_type, TypedDictType):
                # This is the reverse case: `TypedDict | dict`,
                # simply do not allow the reverse checking:
                # do not call `__dict__.__ror__`.
                proper_right_type = get_proper_type(self.accept(e.right))
                if is_named_instance(proper_right_type, "builtins.dict"):
                    use_reverse = USE_REVERSE_NEVER

        if PRECISE_TUPLE_TYPES in self.chk.options.enable_incomplete_feature:
            # Handle tuple[X, ...] + tuple[Y, Z] = tuple[*tuple[X, ...], Y, Z].
            if (
                e.op == "+"
                and isinstance(proper_left_type, Instance)
                and proper_left_type.type.fullname == "builtins.tuple"
            ):
                proper_right_type = get_proper_type(self.accept(e.right))
                if (
                    isinstance(proper_right_type, TupleType)
                    and proper_right_type.partial_fallback.type.fullname == "builtins.tuple"
                    and find_unpack_in_list(proper_right_type.items) is None
                ):
                    return proper_right_type.copy_modified(
                        items=[UnpackType(proper_left_type)] + proper_right_type.items
                    )

        if e.op in operators.op_methods:
            method = operators.op_methods[e.op]
            if use_reverse is UseReverse.DEFAULT or use_reverse is UseReverse.NEVER:
                result, method_type = self.check_op(
                    method,
                    base_type=left_type,
                    arg=e.right,
                    context=e,
                    allow_reverse=use_reverse is UseReverse.DEFAULT,
                )
            elif use_reverse is UseReverse.ALWAYS:
                result, method_type = self.check_op(
                    # The reverse operator here gives better error messages:
                    operators.reverse_op_methods[method],
                    base_type=self.accept(e.right),
                    arg=e.left,
                    context=e,
                    allow_reverse=False,
                )
            else:
                assert_never(use_reverse)
            e.method_type = method_type
            return result
        else:
            raise RuntimeError(f"Unknown operator {e.op}")

    def visit_comparison_expr(self, e: ComparisonExpr) -> Type:
        """Type check a comparison expression.

        Comparison expressions are type checked consecutive-pair-wise
        That is, 'a < b > c == d' is check as 'a < b and b > c and c == d'
        """
        result: Type | None = None
        sub_result: Type

        # Check each consecutive operand pair and their operator
        for left, right, operator in zip(e.operands, e.operands[1:], e.operators):
            left_type = self.accept(left)

            if operator == "in" or operator == "not in":
                # This case covers both iterables and containers, which have different meanings.
                # For a container, the in operator calls the __contains__ method.
                # For an iterable, the in operator iterates over the iterable, and compares each item one-by-one.
                # We allow `in` for a union of containers and iterables as long as at least one of them matches the
                # type of the left operand, as the operation will simply return False if the union's container/iterator
                # type doesn't match the left operand.

                # If the right operand has partial type, look it up without triggering
                # a "Need type annotation ..." message, as it would be noise.
                right_type = self.find_partial_type_ref_fast_path(right)
                if right_type is None:
                    right_type = self.accept(right)  # Validate the right operand

                right_type = get_proper_type(right_type)
                item_types: Sequence[Type] = [right_type]
                if isinstance(right_type, UnionType):
                    item_types = list(right_type.relevant_items())

                sub_result = self.bool_type()

                container_types: list[Type] = []
                iterable_types: list[Type] = []
                failed_out = False
                encountered_partial_type = False

                for item_type in item_types:
                    # Keep track of whether we get type check errors (these won't be reported, they
                    # are just to verify whether something is valid typing wise).
                    with self.msg.filter_errors(save_filtered_errors=True) as container_errors:
                        _, method_type = self.check_method_call_by_name(
                            method="__contains__",
                            base_type=item_type,
                            args=[left],
                            arg_kinds=[ARG_POS],
                            context=e,
                            original_type=right_type,
                        )
                        # Container item type for strict type overlap checks. Note: we need to only
                        # check for nominal type, because a usual "Unsupported operands for in"
                        # will be reported for types incompatible with __contains__().
                        # See testCustomContainsCheckStrictEquality for an example.
                        cont_type = self.chk.analyze_container_item_type(item_type)

                    if isinstance(item_type, PartialType):
                        # We don't really know if this is an error or not, so just shut up.
                        encountered_partial_type = True
                        pass
                    elif (
                        container_errors.has_new_errors()
                        and
                        # is_valid_var_arg is True for any Iterable
                        self.is_valid_var_arg(item_type)
                    ):
                        # it's not a container, but it is an iterable
                        with self.msg.filter_errors(save_filtered_errors=True) as iterable_errors:
                            _, itertype = self.chk.analyze_iterable_item_type_without_expression(
                                item_type, e
                            )
                        if iterable_errors.has_new_errors():
                            self.msg.add_errors(iterable_errors.filtered_errors())
                            failed_out = True
                        else:
                            method_type = CallableType(
                                [left_type],
                                [nodes.ARG_POS],
                                [None],
                                self.bool_type(),
                                self.named_type("builtins.function"),
                            )
                            e.method_types.append(method_type)
                            iterable_types.append(itertype)
                    elif not container_errors.has_new_errors() and cont_type:
                        container_types.append(cont_type)
                        e.method_types.append(method_type)
                    else:
                        self.msg.add_errors(container_errors.filtered_errors())
                        failed_out = True

                if not encountered_partial_type and not failed_out:
                    iterable_type = UnionType.make_union(iterable_types)
                    if not is_subtype(left_type, iterable_type):
                        if not container_types:
                            self.msg.unsupported_operand_types("in", left_type, right_type, e)
                        else:
                            container_type = UnionType.make_union(container_types)
                            if self.dangerous_comparison(
                                left_type,
                                container_type,
                                original_container=right_type,
                                prefer_literal=False,
                            ):
                                self.msg.dangerous_comparison(
                                    left_type, container_type, "container", e
                                )

            elif operator in operators.op_methods:
                method = operators.op_methods[operator]

                with ErrorWatcher(self.msg.errors) as w:
                    sub_result, method_type = self.check_op(
                        method, left_type, right, e, allow_reverse=True
                    )
                    e.method_types.append(method_type)

                # Only show dangerous overlap if there are no other errors. See
                # testCustomEqCheckStrictEquality for an example.
                if not w.has_new_errors() and operator in ("==", "!="):
                    right_type = self.accept(right)
                    if self.dangerous_comparison(left_type, right_type):
                        # Show the most specific literal types possible
                        left_type = try_getting_literal(left_type)
                        right_type = try_getting_literal(right_type)
                        self.msg.dangerous_comparison(left_type, right_type, "equality", e)

            elif operator == "is" or operator == "is not":
                right_type = self.accept(right)  # validate the right operand
                sub_result = self.bool_type()
                if self.dangerous_comparison(left_type, right_type):
                    # Show the most specific literal types possible
                    left_type = try_getting_literal(left_type)
                    right_type = try_getting_literal(right_type)
                    self.msg.dangerous_comparison(left_type, right_type, "identity", e)
                e.method_types.append(None)
            else:
                raise RuntimeError(f"Unknown comparison operator {operator}")

            #  Determine type of boolean-and of result and sub_result
            if result is None:
                result = sub_result
            else:
                result = join.join_types(result, sub_result)

        assert result is not None
        return result

    def find_partial_type_ref_fast_path(self, expr: Expression) -> Type | None:
        """If expression has a partial generic type, return it without additional checks.

        In particular, this does not generate an error about a missing annotation.

        Otherwise, return None.
        """
        if not isinstance(expr, RefExpr):
            return None
        if isinstance(expr.node, Var):
            result = self.analyze_var_ref(expr.node, expr)
            if isinstance(result, PartialType) and result.type is not None:
                self.chk.store_type(expr, fixup_partial_type(result))
                return result
        return None

    def dangerous_comparison(
        self,
        left: Type,
        right: Type,
        *,
        original_container: Type | None = None,
        seen_types: set[tuple[Type, Type]] | None = None,
        prefer_literal: bool = True,
    ) -> bool:
        """Check for dangerous non-overlapping comparisons like 42 == 'no'.

        The original_container is the original container type for 'in' checks
        (and None for equality checks).

        Rules:
            * X and None are overlapping even in strict-optional mode. This is to allow
            'assert x is not None' for x defined as 'x = None  # type: str' in class body
            (otherwise mypy itself would have couple dozen errors because of this).
            * Optional[X] and Optional[Y] are non-overlapping if X and Y are
            non-overlapping, although technically None is overlap, it is most
            likely an error.
            * Any overlaps with everything, i.e. always safe.
            * Special case: b'abc' in b'cde' is safe.
        """
        if not self.chk.options.strict_equality:
            return False

        if seen_types is None:
            seen_types = set()
        if (left, right) in seen_types:
            return False
        seen_types.add((left, right))

        left, right = get_proper_types((left, right))

        # We suppress the error if there is a custom __eq__() method on either
        # side. User defined (or even standard library) classes can define this
        # to return True for comparisons between non-overlapping types.
        if custom_special_method(left, "__eq__") or custom_special_method(right, "__eq__"):
            return False

        if prefer_literal:
            # Also flag non-overlapping literals in situations like:
            #    x: Literal['a', 'b']
            #    if x == 'c':
            #        ...
            left = try_getting_literal(left)
            right = try_getting_literal(right)

        if self.chk.binder.is_unreachable_warning_suppressed():
            # We are inside a function that contains type variables with value restrictions in
            # its signature. In this case we just suppress all strict-equality checks to avoid
            # false positives for code like:
            #
            #     T = TypeVar('T', str, int)
            #     def f(x: T) -> T:
            #         if x == 0:
            #             ...
            #         return x
            #
            # TODO: find a way of disabling the check only for types resulted from the expansion.
            return False
        if isinstance(left, NoneType) or isinstance(right, NoneType):
            return False
        if isinstance(left, UnionType) and isinstance(right, UnionType):
            left = remove_optional(left)
            right = remove_optional(right)
            left, right = get_proper_types((left, right))
        if (
            original_container
            and has_bytes_component(original_container)
            and has_bytes_component(left)
        ):
            # We need to special case bytes and bytearray, because 97 in b'abc', b'a' in b'abc',
            # b'a' in bytearray(b'abc') etc. all return True (and we want to show the error only
            # if the check can _never_ be True).
            return False
        if isinstance(left, Instance) and isinstance(right, Instance):
            # Special case some builtin implementations of AbstractSet.
            left_name = left.type.fullname
            right_name = right.type.fullname
            if (
                left_name in OVERLAPPING_TYPES_ALLOWLIST
                and right_name in OVERLAPPING_TYPES_ALLOWLIST
            ):
                abstract_set = self.chk.lookup_typeinfo("typing.AbstractSet")
                left = map_instance_to_supertype(left, abstract_set)
                right = map_instance_to_supertype(right, abstract_set)
                return self.dangerous_comparison(
                    left.args[0], right.args[0], seen_types=seen_types
                )
            elif left.type.has_base("typing.Mapping") and right.type.has_base("typing.Mapping"):
                # Similar to above: Mapping ignores the classes, it just compares items.
                abstract_map = self.chk.lookup_typeinfo("typing.Mapping")
                left = map_instance_to_supertype(left, abstract_map)
                right = map_instance_to_supertype(right, abstract_map)
                return self.dangerous_comparison(
                    left.args[0], right.args[0], seen_types=seen_types
                ) or self.dangerous_comparison(left.args[1], right.args[1], seen_types=seen_types)
            elif left_name in ("builtins.list", "builtins.tuple") and right_name == left_name:
                return self.dangerous_comparison(
                    left.args[0], right.args[0], seen_types=seen_types
                )
            elif left_name in OVERLAPPING_BYTES_ALLOWLIST and right_name in (
                OVERLAPPING_BYTES_ALLOWLIST
            ):
                return False
        if isinstance(left, LiteralType) and isinstance(right, LiteralType):
            if isinstance(left.value, bool) and isinstance(right.value, bool):
                # Comparing different booleans is not dangerous.
                return False
        if isinstance(left, LiteralType) and isinstance(right, Instance):
            # bytes/bytearray comparisons are supported
            if left.fallback.type.fullname == "builtins.bytes" and right.type.has_base(
                "builtins.bytearray"
            ):
                return False
        if isinstance(right, LiteralType) and isinstance(left, Instance):
            # bytes/bytearray comparisons are supported
            if right.fallback.type.fullname == "builtins.bytes" and left.type.has_base(
                "builtins.bytearray"
            ):
                return False
        return not is_overlapping_types(left, right, ignore_promotions=False)

    def check_method_call_by_name(
        self,
        method: str,
        base_type: Type,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        original_type: Type | None = None,
        self_type: Type | None = None,
    ) -> tuple[Type, Type]:
        """Type check a call to a named method on an object.

        Return tuple (result type, inferred method type). The 'original_type'
        is used for error messages. The self_type is to bind self in methods
        (see analyze_member_access for more details).
        """
        original_type = original_type or base_type
        self_type = self_type or base_type
        # Unions are special-cased to allow plugins to act on each element of the union.
        base_type = get_proper_type(base_type)
        if isinstance(base_type, UnionType):
            return self.check_union_method_call_by_name(
                method, base_type, args, arg_kinds, context, original_type
            )

        method_type = analyze_member_access(
            method,
            base_type,
            context,
            is_lvalue=False,
            is_super=False,
            is_operator=True,
            original_type=original_type,
            self_type=self_type,
            chk=self.chk,
            in_literal_context=self.is_literal_context(),
        )
        return self.check_method_call(method, base_type, method_type, args, arg_kinds, context)

    def check_union_method_call_by_name(
        self,
        method: str,
        base_type: UnionType,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
        original_type: Type | None = None,
    ) -> tuple[Type, Type]:
        """Type check a call to a named method on an object with union type.

        This essentially checks the call using check_method_call_by_name() for each
        union item and unions the result. We do this to allow plugins to act on
        individual union items.
        """
        res: list[Type] = []
        meth_res: list[Type] = []
        for typ in base_type.relevant_items():
            # Format error messages consistently with
            # mypy.checkmember.analyze_union_member_access().
            with self.msg.disable_type_names():
                item, meth_item = self.check_method_call_by_name(
                    method, typ, args, arg_kinds, context, original_type
                )
            res.append(item)
            meth_res.append(meth_item)
        return make_simplified_union(res), make_simplified_union(meth_res)

    def check_method_call(
        self,
        method_name: str,
        base_type: Type,
        method_type: Type,
        args: list[Expression],
        arg_kinds: list[ArgKind],
        context: Context,
    ) -> tuple[Type, Type]:
        """Type check a call to a method with the given name and type on an object.

        Return tuple (result type, inferred method type).
        """
        callable_name = self.method_fullname(base_type, method_name)
        object_type = base_type if callable_name is not None else None

        # Try to refine the method signature using plugin hooks before checking the call.
        method_type = self.transform_callee_type(
            callable_name, method_type, args, arg_kinds, context, object_type=object_type
        )

        return self.check_call(
            method_type,
            args,
            arg_kinds,
            context,
            callable_name=callable_name,
            object_type=base_type,
        )

    def check_op_reversible(
        self,
        op_name: str,
        left_type: Type,
        left_expr: Expression,
        right_type: Type,
        right_expr: Expression,
        context: Context,
    ) -> tuple[Type, Type]:
        def lookup_operator(op_name: str, base_type: Type) -> Type | None:
            """Looks up the given operator and returns the corresponding type,
            if it exists."""

            # This check is an important performance optimization.
            if not has_operator(base_type, op_name, self.named_type):
                return None

            with self.msg.filter_errors() as w:
                member = analyze_member_access(
                    name=op_name,
                    typ=base_type,
                    is_lvalue=False,
                    is_super=False,
                    is_operator=True,
                    original_type=base_type,
                    context=context,
                    chk=self.chk,
                    in_literal_context=self.is_literal_context(),
                )
                return None if w.has_new_errors() else member

        def lookup_definer(typ: Instance, attr_name: str) -> str | None:
            """Returns the name of the class that contains the actual definition of attr_name.

            So if class A defines foo and class B subclasses A, running
            'get_class_defined_in(B, "foo")` would return the full name of A.

            However, if B were to override and redefine foo, that method call would
            return the full name of B instead.

            If the attr name is not present in the given class or its MRO, returns None.
            """
            for cls in typ.type.mro:
                if cls.names.get(attr_name):
                    return cls.fullname
            return None

        left_type = get_proper_type(left_type)
        right_type = get_proper_type(right_type)

        # If either the LHS or the RHS are Any, we can't really concluding anything
        # about the operation since the Any type may or may not define an
        # __op__ or __rop__ method. So, we punt and return Any instead.

        if isinstance(left_type, AnyType):
            any_type = AnyType(TypeOfAny.from_another_any, source_any=left_type)
            return any_type, any_type
        if isinstance(right_type, AnyType):
            any_type = AnyType(TypeOfAny.from_another_any, source_any=right_type)
            return any_type, any_type

        # STEP 1:
        # We start by getting the __op__ and __rop__ methods, if they exist.

        rev_op_name = operators.reverse_op_methods[op_name]

        left_op = lookup_operator(op_name, left_type)
        right_op = lookup_operator(rev_op_name, right_type)

        # STEP 2a:
        # We figure out in which order Python will call the operator methods. As it
        # turns out, it's not as simple as just trying to call __op__ first and
        # __rop__ second.
        #
        # We store the determined order inside the 'variants_raw' variable,
        # which records tuples containing the method, base type, and the argument.

        if op_name in operators.op_methods_that_shortcut and is_same_type(left_type, right_type):
            # When we do "A() + A()", for example, Python will only call the __add__ method,
            # never the __radd__ method.
            #
            # This is the case even if the __add__ method is completely missing and the __radd__
            # method is defined.

            variants_raw = [(op_name, left_op, left_type, right_expr)]
        elif (
            is_subtype(right_type, left_type)
            and isinstance(left_type, Instance)
            and isinstance(right_type, Instance)
            and not (
                left_type.type.alt_promote is not None
                and left_type.type.alt_promote.type is right_type.type
            )
            and lookup_definer(left_type, op_name) != lookup_definer(right_type, rev_op_name)
        ):
            # When we do "A() + B()" where B is a subclass of A, we'll actually try calling
            # B's __radd__ method first, but ONLY if B explicitly defines or overrides the
            # __radd__ method.
            #
            # This mechanism lets subclasses "refine" the expected outcome of the operation, even
            # if they're located on the RHS.
            #
            # As a special case, the alt_promote check makes sure that we don't use the
            # __radd__ method of int if the LHS is a native int type.

            variants_raw = [
                (rev_op_name, right_op, right_type, left_expr),
                (op_name, left_op, left_type, right_expr),
            ]
        else:
            # In all other cases, we do the usual thing and call __add__ first and
            # __radd__ second when doing "A() + B()".

            variants_raw = [
                (op_name, left_op, left_type, right_expr),
                (rev_op_name, right_op, right_type, left_expr),
            ]

        # STEP 3:
        # We now filter out all non-existent operators. The 'variants' list contains
        # all operator methods that are actually present, in the order that Python
        # attempts to invoke them.

        variants = [(na, op, obj, arg) for (na, op, obj, arg) in variants_raw if op is not None]

        # STEP 4:
        # We now try invoking each one. If an operation succeeds, end early and return
        # the corresponding result. Otherwise, return the result and errors associated
        # with the first entry.

        errors = []
        results = []
        for name, method, obj, arg in variants:
            with self.msg.filter_errors(save_filtered_errors=True) as local_errors:
                result = self.check_method_call(name, obj, method, [arg], [ARG_POS], context)
            if local_errors.has_new_errors():
                errors.append(local_errors.filtered_errors())
                results.append(result)
            else:
                if isinstance(obj, Instance) and isinstance(
                    defn := obj.type.get_method(name), OverloadedFuncDef
                ):
                    for item in defn.items:
                        if (
                            isinstance(item, Decorator)
                            and isinstance(typ := item.func.type, CallableType)
                            and bind_self(typ) == result[1]
                        ):
                            self.chk.check_deprecated(item.func, context)
                return result

        # We finish invoking above operators and no early return happens. Therefore,
        # we check if either the LHS or the RHS is Instance and fallbacks to Any,
        # if so, we also return Any
        if (isinstance(left_type, Instance) and left_type.type.fallback_to_any) or (
            isinstance(right_type, Instance) and right_type.type.fallback_to_any
        ):
            any_type = AnyType(TypeOfAny.special_form)
            return any_type, any_type

        # STEP 4b:
        # Sometimes, the variants list is empty. In that case, we fall-back to attempting to
        # call the __op__ method (even though it's missing).

        if not variants:
            with self.msg.filter_errors(save_filtered_errors=True) as local_errors:
                result = self.check_method_call_by_name(
                    op_name, left_type, [right_expr], [ARG_POS], context
                )

            if local_errors.has_new_errors():
                errors.append(local_errors.filtered_errors())
                results.append(result)
            else:
                # Although we should not need this case anymore, we keep it just in case, as
                # otherwise we will get a crash if we introduce inconsistency in checkmember.py
                return result

        self.msg.add_errors(errors[0])
        if len(results) == 1:
            return results[0]
        else:
            error_any = AnyType(TypeOfAny.from_error)
            result = error_any, error_any
            return result

    def check_op(
        self,
        method: str,
        base_type: Type,
        arg: Expression,
        context: Context,
        allow_reverse: bool = False,
    ) -> tuple[Type, Type]:
        """Type check a binary operation which maps to a method call.

        Return tuple (result type, inferred operator method type).
        """

        if allow_reverse:
            left_variants = [base_type]
            base_type = get_proper_type(base_type)
            if isinstance(base_type, UnionType):
                left_variants = list(flatten_nested_unions(base_type.relevant_items()))
            right_type = self.accept(arg)

            # Step 1: We first try leaving the right arguments alone and destructure
            # just the left ones. (Mypy can sometimes perform some more precise inference
            # if we leave the right operands a union -- see testOperatorWithEmptyListAndSum.)
            all_results = []
            all_inferred = []

            with self.msg.filter_errors() as local_errors:
                for left_possible_type in left_variants:
                    result, inferred = self.check_op_reversible(
                        op_name=method,
                        left_type=left_possible_type,
                        left_expr=TempNode(left_possible_type, context=context),
                        right_type=right_type,
                        right_expr=arg,
                        context=context,
                    )
                    all_results.append(result)
                    all_inferred.append(inferred)

            if not local_errors.has_new_errors():
                results_final = make_simplified_union(all_results)
                inferred_final = make_simplified_union(all_inferred)
                return results_final, inferred_final

            # Step 2: If that fails, we try again but also destructure the right argument.
            # This is also necessary to make certain edge cases work -- see
            # testOperatorDoubleUnionInterwovenUnionAdd, for example.

            # Note: We want to pass in the original 'arg' for 'left_expr' and 'right_expr'
            # whenever possible so that plugins and similar things can introspect on the original
            # node if possible.
            #
            # We don't do the same for the base expression because it could lead to weird
            # type inference errors -- e.g. see 'testOperatorDoubleUnionSum'.
            # TODO: Can we use `type_overrides_set()` here?
            right_variants = [(right_type, arg)]
            right_type = get_proper_type(right_type)
            if isinstance(right_type, UnionType):
                right_variants = [
                    (item, TempNode(item, context=context))
                    for item in flatten_nested_unions(right_type.relevant_items())
                ]

            all_results = []
            all_inferred = []

            with self.msg.filter_errors(save_filtered_errors=True) as local_errors:
                for left_possible_type in left_variants:
                    for right_possible_type, right_expr in right_variants:
                        result, inferred = self.check_op_reversible(
                            op_name=method,
                            left_type=left_possible_type,
                            left_expr=TempNode(left_possible_type, context=context),
                            right_type=right_possible_type,
                            right_expr=right_expr,
                            context=context,
                        )
                        all_results.append(result)
                        all_inferred.append(inferred)

            if local_errors.has_new_errors():
                self.msg.add_errors(local_errors.filtered_errors())
                # Point any notes to the same location as an existing message.
                err = local_errors.filtered_errors()[-1]
                recent_context = TempNode(NoneType())
                recent_context.line = err.line
                recent_context.column = err.column
                if len(left_variants) >= 2 and len(right_variants) >= 2:
                    self.msg.warn_both_operands_are_from_unions(recent_context)
                elif len(left_variants) >= 2:
                    self.msg.warn_operand_was_from_union("Left", base_type, context=recent_context)
                elif len(right_variants) >= 2:
                    self.msg.warn_operand_was_from_union(
                        "Right", right_type, context=recent_context
                    )

            # See the comment in 'check_overload_call' for more details on why
            # we call 'combine_function_signature' instead of just unioning the inferred
            # callable types.
            results_final = make_simplified_union(all_results)
            inferred_final = self.combine_function_signatures(get_proper_types(all_inferred))
            return results_final, inferred_final
        else:
            return self.check_method_call_by_name(
                method=method,
                base_type=base_type,
                args=[arg],
                arg_kinds=[ARG_POS],
                context=context,
            )

    def check_boolean_op(self, e: OpExpr) -> Type:
        """Type check a boolean operation ('and' or 'or')."""

        # A boolean operation can evaluate to either of the operands.

        # We use the current type context to guide the type inference of
        # the left operand. We also use the left operand type to guide the type
        # inference of the right operand so that expressions such as
        # '[1] or []' are inferred correctly.
        ctx = self.type_context[-1]
        left_type = self.accept(e.left, ctx)
        expanded_left_type = try_expanding_sum_type_to_union(left_type, "builtins.bool")

        assert e.op in ("and", "or")  # Checked by visit_op_expr

        if e.right_always:
            left_map: mypy.checker.TypeMap = None
            right_map: mypy.checker.TypeMap = {}
        elif e.right_unreachable:
            left_map, right_map = {}, None
        elif e.op == "and":
            right_map, left_map = self.chk.find_isinstance_check(e.left)
        elif e.op == "or":
            left_map, right_map = self.chk.find_isinstance_check(e.left)

        # If left_map is None then we know mypy considers the left expression
        # to be redundant.
        if (
            codes.REDUNDANT_EXPR in self.chk.options.enabled_error_codes
            and left_map is None
            # don't report an error if it's intentional
            and not e.right_always
        ):
            self.msg.redundant_left_operand(e.op, e.left)

        if (
            self.chk.should_report_unreachable_issues()
            and right_map is None
            # don't report an error if it's intentional
            and not e.right_unreachable
        ):
            self.msg.unreachable_right_operand(e.op, e.right)

        right_type = self.analyze_cond_branch(
            right_map, e.right, self._combined_context(expanded_left_type)
        )

        if left_map is None and right_map is None:
            return UninhabitedType()

        if right_map is None:
            # The boolean expression is statically known to be the left value
            assert left_map is not None
            return left_type
        if left_map is None:
            # The boolean expression is statically known to be the right value
            assert right_map is not None
            return right_type

        if e.op == "and":
            restricted_left_type = false_only(expanded_left_type)
            result_is_left = not expanded_left_type.can_be_true
        elif e.op == "or":
            restricted_left_type = true_only(expanded_left_type)
            result_is_left = not expanded_left_type.can_be_false

        if isinstance(restricted_left_type, UninhabitedType):
            # The left operand can never be the result
            return right_type
        elif result_is_left:
            # The left operand is always the result
            return left_type
        else:
            return make_simplified_union([restricted_left_type, right_type])

    def check_list_multiply(self, e: OpExpr) -> Type:
        """Type check an expression of form '[...] * e'.

        Type inference is special-cased for this common construct.
        """
        right_type = self.accept(e.right)
        if is_subtype(right_type, self.named_type("builtins.int")):
            # Special case: [...] * <int value>. Use the type context of the
            # OpExpr, since the multiplication does not affect the type.
            left_type = self.accept(e.left, type_context=self.type_context[-1])
        else:
            left_type = self.accept(e.left)
        result, method_type = self.check_op("__mul__", left_type, e.right, e)
        e.method_type = method_type
        return result

    def visit_assignment_expr(self, e: AssignmentExpr) -> Type:
        value = self.accept(e.value)
        self.chk.check_assignment(e.target, e.value)
        self.chk.check_final(e)
        if not has_uninhabited_component(value):
            # TODO: can we get rid of this extra store_type()?
            # Usually, check_assignment() already stores the lvalue type correctly.
            self.chk.store_type(e.target, value)
        self.find_partial_type_ref_fast_path(e.target)
        return value

    def visit_unary_expr(self, e: UnaryExpr) -> Type:
        """Type check an unary operation ('not', '-', '+' or '~')."""
        operand_type = self.accept(e.expr)
        op = e.op
        if op == "not":
            result: Type = self.bool_type()
            self.chk.check_for_truthy_type(operand_type, e.expr)
        else:
            method = operators.unary_op_methods[op]
            result, method_type = self.check_method_call_by_name(method, operand_type, [], [], e)
            e.method_type = method_type
        return result

    def visit_index_expr(self, e: IndexExpr) -> Type:
        """Type check an index expression (base[index]).

        It may also represent type application.
        """
        result = self.visit_index_expr_helper(e)
        result = self.narrow_type_from_binder(e, result)
        p_result = get_proper_type(result)
        if (
            self.is_literal_context()
            and isinstance(p_result, Instance)
            and p_result.last_known_value is not None
        ):
            result = p_result.last_known_value
        return result

    def visit_index_expr_helper(self, e: IndexExpr) -> Type:
        if e.analyzed:
            # It's actually a type application.
            return self.accept(e.analyzed)
        left_type = self.accept(e.base)
        return self.visit_index_with_type(left_type, e)

    def visit_index_with_type(
        self,
        left_type: Type,
        e: IndexExpr,
        original_type: ProperType | None = None,
        self_type: Type | None = None,
    ) -> Type:
        """Analyze type of an index expression for a given type of base expression.

        The 'original_type' is used for error messages (currently used for union types). The
        'self_type' is to bind self in methods (see analyze_member_access for more details).
        """
        index = e.index
        self_type = self_type or left_type
        left_type = get_proper_type(left_type)

        # Visit the index, just to make sure we have a type for it available
        self.accept(index)

        if isinstance(left_type, TupleType) and any(
            isinstance(it, UnpackType) for it in left_type.items
        ):
            # Normalize variadic tuples for consistency.
            left_type = expand_type(left_type, {})

        if isinstance(left_type, UnionType):
            original_type = original_type or left_type
            # Don't combine literal types, since we may need them for type narrowing.
            return make_simplified_union(
                [
                    self.visit_index_with_type(typ, e, original_type)
                    for typ in left_type.relevant_items()
                ],
                contract_literals=False,
            )
        elif isinstance(left_type, TupleType) and self.chk.in_checked_function():
            # Special case for tuples. They return a more specific type when
            # indexed by an integer literal.
            if isinstance(index, SliceExpr):
                return self.visit_tuple_slice_helper(left_type, index)

            ns = self.try_getting_int_literals(index)
            if ns is not None:
                out = []
                for n in ns:
                    item = self.visit_tuple_index_helper(left_type, n)
                    if item is not None:
                        out.append(item)
                    else:
                        self.chk.fail(message_registry.TUPLE_INDEX_OUT_OF_RANGE, e)
                        if any(isinstance(t, UnpackType) for t in left_type.items):
                            min_len = self.min_tuple_length(left_type)
                            self.chk.note(f"Variadic tuple can have length {min_len}", e)
                        return AnyType(TypeOfAny.from_error)
                return make_simplified_union(out)
            else:
                return self.nonliteral_tuple_index_helper(left_type, index)
        elif isinstance(left_type, TypedDictType):
            return self.visit_typeddict_index_expr(left_type, e.index)[0]
        elif isinstance(left_type, FunctionLike) and left_type.is_type_obj():
            if left_type.type_object().is_enum:
                return self.visit_enum_index_expr(left_type.type_object(), e.index, e)
            elif (
                left_type.type_object().type_vars
                or left_type.type_object().fullname == "builtins.type"
            ):
                return self.named_type("types.GenericAlias")

        if isinstance(left_type, TypeVarType):
            return self.visit_index_with_type(
                left_type.values_or_bound(), e, original_type, left_type
            )
        elif isinstance(left_type, Instance) and left_type.type.fullname == "typing._SpecialForm":
            # Allow special forms to be indexed and used to create union types
            return self.named_type("typing._SpecialForm")
        else:
            result, method_type = self.check_method_call_by_name(
                "__getitem__",
                left_type,
                [e.index],
                [ARG_POS],
                e,
                original_type=original_type,
                self_type=self_type,
            )
            e.method_type = method_type
            return result

    def min_tuple_length(self, left: TupleType) -> int:
        unpack_index = find_unpack_in_list(left.items)
        if unpack_index is None:
            return left.length()
        unpack = left.items[unpack_index]
        assert isinstance(unpack, UnpackType)
        if isinstance(unpack.type, TypeVarTupleType):
            return left.length() - 1 + unpack.type.min_len
        return left.length() - 1

    def visit_tuple_index_helper(self, left: TupleType, n: int) -> Type | None:
        unpack_index = find_unpack_in_list(left.items)
        if unpack_index is None:
            if n < 0:
                n += len(left.items)
            if 0 <= n < len(left.items):
                return left.items[n]
            return None
        unpack = left.items[unpack_index]
        assert isinstance(unpack, UnpackType)
        unpacked = get_proper_type(unpack.type)
        if isinstance(unpacked, TypeVarTupleType):
            # Usually we say that TypeVarTuple can't be split, be in case of
            # indexing it seems benign to just return the upper bound item, similar
            # to what we do when indexing a regular TypeVar.
            bound = get_proper_type(unpacked.upper_bound)
            assert isinstance(bound, Instance)
            assert bound.type.fullname == "builtins.tuple"
            middle = bound.args[0]
        else:
            assert isinstance(unpacked, Instance)
            assert unpacked.type.fullname == "builtins.tuple"
            middle = unpacked.args[0]

        extra_items = self.min_tuple_length(left) - left.length() + 1
        if n >= 0:
            if n >= self.min_tuple_length(left):
                # For tuple[int, *tuple[str, ...], int] we allow either index 0 or 1,
                # since variadic item may have zero items.
                return None
            if n < unpack_index:
                return left.items[n]
            return UnionType.make_union(
                [middle]
                + left.items[unpack_index + 1 : max(n - extra_items + 2, unpack_index + 1)],
                left.line,
                left.column,
            )
        n += self.min_tuple_length(left)
        if n < 0:
            # Similar to above, we only allow -1, and -2 for tuple[int, *tuple[str, ...], int]
            return None
        if n >= unpack_index + extra_items:
            return left.items[n - extra_items + 1]
        return UnionType.make_union(
            left.items[min(n, unpack_index) : unpack_index] + [middle], left.line, left.column
        )

    def visit_tuple_slice_helper(self, left_type: TupleType, slic: SliceExpr) -> Type:
        begin: Sequence[int | None] = [None]
        end: Sequence[int | None] = [None]
        stride: Sequence[int | None] = [None]

        if slic.begin_index:
            begin_raw = self.try_getting_int_literals(slic.begin_index)
            if begin_raw is None:
                return self.nonliteral_tuple_index_helper(left_type, slic)
            begin = begin_raw

        if slic.end_index:
            end_raw = self.try_getting_int_literals(slic.end_index)
            if end_raw is None:
                return self.nonliteral_tuple_index_helper(left_type, slic)
            end = end_raw

        if slic.stride:
            stride_raw = self.try_getting_int_literals(slic.stride)
            if stride_raw is None:
                return self.nonliteral_tuple_index_helper(left_type, slic)
            stride = stride_raw

        items: list[Type] = []
        for b, e, s in itertools.product(begin, end, stride):
            item = left_type.slice(b, e, s, fallback=self.named_type("builtins.tuple"))
            if item is None:
                self.chk.fail(message_registry.AMBIGUOUS_SLICE_OF_VARIADIC_TUPLE, slic)
                return AnyType(TypeOfAny.from_error)
            items.append(item)
        return make_simplified_union(items)

    def try_getting_int_literals(self, index: Expression) -> list[int] | None:
        """If the given expression or type corresponds to an int literal
        or a union of int literals, returns a list of the underlying ints.
        Otherwise, returns None.

        Specifically, this function is guaranteed to return a list with
        one or more ints if one the following is true:

        1. 'expr' is a IntExpr or a UnaryExpr backed by an IntExpr
        2. 'typ' is a LiteralType containing an int
        3. 'typ' is a UnionType containing only LiteralType of ints
        """
        if isinstance(index, IntExpr):
            return [index.value]
        elif isinstance(index, UnaryExpr):
            if index.op == "-":
                operand = index.expr
                if isinstance(operand, IntExpr):
                    return [-1 * operand.value]
            if index.op == "+":
                operand = index.expr
                if isinstance(operand, IntExpr):
                    return [operand.value]
        typ = get_proper_type(self.accept(index))
        if isinstance(typ, Instance) and typ.last_known_value is not None:
            typ = typ.last_known_value
        if isinstance(typ, LiteralType) and isinstance(typ.value, int):
            return [typ.value]
        if isinstance(typ, UnionType):
            out = []
            for item in get_proper_types(typ.items):
                if isinstance(item, LiteralType) and isinstance(item.value, int):
                    out.append(item.value)
                else:
                    return None
            return out
        return None

    def nonliteral_tuple_index_helper(self, left_type: TupleType, index: Expression) -> Type:
        self.check_method_call_by_name("__getitem__", left_type, [index], [ARG_POS], context=index)
        # We could return the return type from above, but unions are often better than the join
        union = self.union_tuple_fallback_item(left_type)
        if isinstance(index, SliceExpr):
            return self.chk.named_generic_type("builtins.tuple", [union])
        return union

    def union_tuple_fallback_item(self, left_type: TupleType) -> Type:
        # TODO: this duplicates logic in typeops.tuple_fallback().
        items = []
        for item in left_type.items:
            if isinstance(item, UnpackType):
                unpacked_type = get_proper_type(item.type)
                if isinstance(unpacked_type, TypeVarTupleType):
                    unpacked_type = get_proper_type(unpacked_type.upper_bound)
                if (
                    isinstance(unpacked_type, Instance)
                    and unpacked_type.type.fullname == "builtins.tuple"
                ):
                    items.append(unpacked_type.args[0])
                else:
                    raise NotImplementedError
            else:
                items.append(item)
        return make_simplified_union(items)

    def visit_typeddict_index_expr(
        self, td_type: TypedDictType, index: Expression, setitem: bool = False
    ) -> tuple[Type, set[str]]:
        if isinstance(index, StrExpr):
            key_names = [index.value]
        else:
            typ = get_proper_type(self.accept(index))
            if isinstance(typ, UnionType):
                key_types: list[Type] = list(typ.items)
            else:
                key_types = [typ]

            key_names = []
            for key_type in get_proper_types(key_types):
                if isinstance(key_type, Instance) and key_type.last_known_value is not None:
                    key_type = key_type.last_known_value

                if (
                    isinstance(key_type, LiteralType)
                    and isinstance(key_type.value, str)
                    and key_type.fallback.type.fullname != "builtins.bytes"
                ):
                    key_names.append(key_type.value)
                else:
                    self.msg.typeddict_key_must_be_string_literal(td_type, index)
                    return AnyType(TypeOfAny.from_error), set()

        value_types = []
        for key_name in key_names:
            value_type = td_type.items.get(key_name)
            if value_type is None:
                self.msg.typeddict_key_not_found(td_type, key_name, index, setitem)
                return AnyType(TypeOfAny.from_error), set()
            else:
                value_types.append(value_type)
        return make_simplified_union(value_types), set(key_names)

    def visit_enum_index_expr(
        self, enum_type: TypeInfo, index: Expression, context: Context
    ) -> Type:
        string_type: Type = self.named_type("builtins.str")
        self.chk.check_subtype(
            self.accept(index),
            string_type,
            context,
            "Enum index should be a string",
            "actual index type",
        )
        return Instance(enum_type, [])

    def visit_cast_expr(self, expr: CastExpr) -> Type:
        """Type check a cast expression."""
        source_type = self.accept(
            expr.expr,
            type_context=AnyType(TypeOfAny.special_form),
            allow_none_return=True,
            always_allow_any=True,
        )
        target_type = expr.type
        options = self.chk.options
        if (
            options.warn_redundant_casts
            and not is_same_type(target_type, AnyType(TypeOfAny.special_form))
            and is_same_type(source_type, target_type)
        ):
            self.msg.redundant_cast(target_type, expr)
        if options.disallow_any_unimported and has_any_from_unimported_type(target_type):
            self.msg.unimported_type_becomes_any("Target type of cast", target_type, expr)
        check_for_explicit_any(
            target_type, self.chk.options, self.chk.is_typeshed_stub, self.msg, context=expr
        )
        return target_type

    def visit_assert_type_expr(self, expr: AssertTypeExpr) -> Type:
        source_type = self.accept(
            expr.expr,
            type_context=self.type_context[-1],
            allow_none_return=True,
            always_allow_any=True,
        )
        if self.chk.current_node_deferred:
            return source_type

        target_type = expr.type
        proper_source_type = get_proper_type(source_type)
        if (
            isinstance(proper_source_type, mypy.types.Instance)
            and proper_source_type.last_known_value is not None
        ):
            source_type = proper_source_type.last_known_value
        if not is_same_type(source_type, target_type):
            if not self.chk.in_checked_function():
                self.msg.note(
                    '"assert_type" expects everything to be "Any" in unchecked functions',
                    expr.expr,
                )
            self.msg.assert_type_fail(source_type, target_type, expr)
        return source_type

    def visit_reveal_expr(self, expr: RevealExpr) -> Type:
        """Type check a reveal_type expression."""
        if expr.kind == REVEAL_TYPE:
            assert expr.expr is not None
            revealed_type = self.accept(
                expr.expr, type_context=self.type_context[-1], allow_none_return=True
            )
            if not self.chk.current_node_deferred:
                self.msg.reveal_type(revealed_type, expr.expr)
                if not self.chk.in_checked_function():
                    self.msg.note(
                        "'reveal_type' always outputs 'Any' in unchecked functions", expr.expr
                    )
                self.check_reveal_imported(expr)
            return revealed_type
        else:
            # REVEAL_LOCALS
            if not self.chk.current_node_deferred:
                # the RevealExpr contains a local_nodes attribute,
                # calculated at semantic analysis time. Use it to pull out the
                # corresponding subset of variables in self.chk.type_map
                names_to_types = (
                    {var_node.name: var_node.type for var_node in expr.local_nodes}
                    if expr.local_nodes is not None
                    else {}
                )

                self.msg.reveal_locals(names_to_types, expr)
                self.check_reveal_imported(expr)
            return NoneType()

    def check_reveal_imported(self, expr: RevealExpr) -> None:
        if codes.UNIMPORTED_REVEAL not in self.chk.options.enabled_error_codes:
            return

        name = ""
        if expr.kind == REVEAL_LOCALS:
            name = "reveal_locals"
        elif expr.kind == REVEAL_TYPE and not expr.is_imported:
            name = "reveal_type"
        else:
            return

        self.chk.fail(f'Name "{name}" is not defined', expr, code=codes.UNIMPORTED_REVEAL)
        if name == "reveal_type":
            module = (
                "typing" if self.chk.options.python_version >= (3, 11) else "typing_extensions"
            )
            hint = (
                'Did you forget to import it from "{module}"?'
                ' (Suggestion: "from {module} import {name}")'
            ).format(module=module, name=name)
            self.chk.note(hint, expr, code=codes.UNIMPORTED_REVEAL)

    def visit_type_application(self, tapp: TypeApplication) -> Type:
        """Type check a type application (expr[type, ...]).

        There are two different options here, depending on whether expr refers
        to a type alias or directly to a generic class. In the first case we need
        to use a dedicated function typeanal.instantiate_type_alias(). This
        is due to slight differences in how type arguments are applied and checked.
        """
        if isinstance(tapp.expr, RefExpr) and isinstance(tapp.expr.node, TypeAlias):
            if tapp.expr.node.python_3_12_type_alias:
                return self.type_alias_type_type()
            # Subscription of a (generic) alias in runtime context, expand the alias.
            item = instantiate_type_alias(
                tapp.expr.node,
                tapp.types,
                self.chk.fail,
                tapp.expr.node.no_args,
                tapp,
                self.chk.options,
            )
            item = get_proper_type(item)
            if isinstance(item, Instance):
                tp = type_object_type(item.type, self.named_type)
                return self.apply_type_arguments_to_callable(tp, item.args, tapp)
            elif isinstance(item, TupleType) and item.partial_fallback.type.is_named_tuple:
                tp = type_object_type(item.partial_fallback.type, self.named_type)
                return self.apply_type_arguments_to_callable(tp, item.partial_fallback.args, tapp)
            elif isinstance(item, TypedDictType):
                return self.typeddict_callable_from_context(item)
            else:
                self.chk.fail(message_registry.ONLY_CLASS_APPLICATION, tapp)
                return AnyType(TypeOfAny.from_error)
        # Type application of a normal generic class in runtime context.
        # This is typically used as `x = G[int]()`.
        tp = get_proper_type(self.accept(tapp.expr))
        if isinstance(tp, (CallableType, Overloaded)):
            if not tp.is_type_obj():
                self.chk.fail(message_registry.ONLY_CLASS_APPLICATION, tapp)
            return self.apply_type_arguments_to_callable(tp, tapp.types, tapp)
        if isinstance(tp, AnyType):
            return AnyType(TypeOfAny.from_another_any, source_any=tp)
        return AnyType(TypeOfAny.special_form)

    def visit_type_alias_expr(self, alias: TypeAliasExpr) -> Type:
        """Right hand side of a type alias definition.

        It has the same type as if the alias itself was used in a runtime context.
        For example, here:

            A = reveal_type(List[T])
            reveal_type(A)

        both `reveal_type` instances will reveal the same type `def (...) -> builtins.list[Any]`.
        Note that type variables are implicitly substituted with `Any`.
        """
        return self.alias_type_in_runtime_context(alias.node, ctx=alias, alias_definition=True)

    def alias_type_in_runtime_context(
        self, alias: TypeAlias, *, ctx: Context, alias_definition: bool = False
    ) -> Type:
        """Get type of a type alias (could be generic) in a runtime expression.

        Note that this function can be called only if the alias appears _not_
        as a target of type application, which is treated separately in the
        visit_type_application method. Some examples where this method is called are
        casts and instantiation:

            class LongName(Generic[T]): ...
            A = LongName[int]

            x = A()
            y = cast(A, ...)
        """
        if alias.python_3_12_type_alias:
            return self.type_alias_type_type()
        if isinstance(alias.target, Instance) and alias.target.invalid:  # type: ignore[misc]
            # An invalid alias, error already has been reported
            return AnyType(TypeOfAny.from_error)
        # If this is a generic alias, we set all variables to `Any`.
        # For example:
        #     A = List[Tuple[T, T]]
        #     x = A() <- same as List[Tuple[Any, Any]], see PEP 484.
        disallow_any = self.chk.options.disallow_any_generics and self.is_callee
        item = get_proper_type(
            set_any_tvars(
                alias,
                [],
                ctx.line,
                ctx.column,
                self.chk.options,
                disallow_any=disallow_any,
                fail=self.msg.fail,
            )
        )
        if isinstance(item, Instance):
            # Normally we get a callable type (or overloaded) with .is_type_obj() true
            # representing the class's constructor
            tp = type_object_type(item.type, self.named_type)
            if alias.no_args:
                return tp
            return self.apply_type_arguments_to_callable(tp, item.args, ctx)
        elif (
            isinstance(item, TupleType)
            and
            # Tuple[str, int]() fails at runtime, only named tuples and subclasses work.
            tuple_fallback(item).type.fullname != "builtins.tuple"
        ):
            return type_object_type(tuple_fallback(item).type, self.named_type)
        elif isinstance(item, TypedDictType):
            return self.typeddict_callable_from_context(item)
        elif isinstance(item, NoneType):
            return TypeType(item, line=item.line, column=item.column)
        elif isinstance(item, AnyType):
            return AnyType(TypeOfAny.from_another_any, source_any=item)
        elif (
            isinstance(item, UnionType)
            and item.uses_pep604_syntax
            and self.chk.options.python_version >= (3, 10)
        ):
            return self.chk.named_generic_type("types.UnionType", item.items)
        else:
            if alias_definition:
                return AnyType(TypeOfAny.special_form)
            # The _SpecialForm type can be used in some runtime contexts (e.g. it may have __or__).
            return self.named_type("typing._SpecialForm")

    def split_for_callable(
        self, t: CallableType, args: Sequence[Type], ctx: Context
    ) -> list[Type]:
        """Handle directly applying type arguments to a variadic Callable.

        This is needed in situations where e.g. variadic class object appears in
        runtime context. For example:
            class C(Generic[T, Unpack[Ts]]): ...
            x = C[int, str]()

        We simply group the arguments that need to go into Ts variable into a TupleType,
        similar to how it is done in other places using split_with_prefix_and_suffix().
        """
        if t.is_type_obj():
            # Type arguments must map to class type variables, ignoring constructor vars.
            vars = t.type_object().defn.type_vars
        else:
            vars = list(t.variables)
        args = flatten_nested_tuples(args)

        # TODO: this logic is duplicated with semanal_typeargs.
        for tv, arg in zip(t.variables, args):
            if isinstance(tv, ParamSpecType):
                if not isinstance(
                    get_proper_type(arg), (Parameters, ParamSpecType, AnyType, UnboundType)
                ):
                    self.chk.fail(
                        "Can only replace ParamSpec with a parameter types list or"
                        f" another ParamSpec, got {format_type(arg, self.chk.options)}",
                        ctx,
                    )
                    return [AnyType(TypeOfAny.from_error)] * len(vars)

        if not vars or not any(isinstance(v, TypeVarTupleType) for v in vars):
            return list(args)
        # TODO: in future we may want to support type application to variadic functions.
        assert t.is_type_obj()
        info = t.type_object()
        # We reuse the logic from semanal phase to reduce code duplication.
        fake = Instance(info, args, line=ctx.line, column=ctx.column)
        # This code can be only called either from checking a type application, or from
        # checking a type alias (after the caller handles no_args aliases), so we know it
        # was initially an IndexExpr, and we allow empty tuple type arguments.
        if not validate_instance(fake, self.chk.fail, empty_tuple_index=True):
            fix_instance(
                fake, self.chk.fail, self.chk.note, disallow_any=False, options=self.chk.options
            )
            args = list(fake.args)

        prefix = next(i for (i, v) in enumerate(vars) if isinstance(v, TypeVarTupleType))
        suffix = len(vars) - prefix - 1
        tvt = vars[prefix]
        assert isinstance(tvt, TypeVarTupleType)
        start, middle, end = split_with_prefix_and_suffix(tuple(args), prefix, suffix)
        return list(start) + [TupleType(list(middle), tvt.tuple_fallback)] + list(end)

    def apply_type_arguments_to_callable(
        self, tp: Type, args: Sequence[Type], ctx: Context
    ) -> Type:
        """Apply type arguments to a generic callable type coming from a type object.

        This will first perform type arguments count checks, report the
        error as needed, and return the correct kind of Any. As a special
        case this returns Any for non-callable types, because if type object type
        is not callable, then an error should be already reported.
        """
        tp = get_proper_type(tp)

        if isinstance(tp, CallableType):
            if tp.is_type_obj():
                # If we have a class object in runtime context, then the available type
                # variables are those of the class, we don't include additional variables
                # of the constructor. So that with
                #     class C(Generic[T]):
                #         def __init__(self, f: Callable[[S], T], x: S) -> None
                # C[int] is valid
                # C[int, str] is invalid (although C as a callable has 2 type variables)
                # Note: various logic below and in applytype.py relies on the fact that
                # class type variables appear *before* constructor variables.
                type_vars = tp.type_object().defn.type_vars
            else:
                type_vars = list(tp.variables)
            min_arg_count = sum(not v.has_default() for v in type_vars)
            has_type_var_tuple = any(isinstance(v, TypeVarTupleType) for v in type_vars)
            if (
                len(args) < min_arg_count or len(args) > len(type_vars)
            ) and not has_type_var_tuple:
                if tp.is_type_obj() and tp.type_object().fullname == "builtins.tuple":
                    # e.g. expression tuple[X, Y]
                    # - want the type of the expression i.e. a function with that as its return type
                    # - tp is type of tuple (note it won't have params as we are only called
                    #   with generic callable type)
                    # - tuple[X, Y]() takes a single arg that is a tuple containing an X and a Y
                    return CallableType(
                        [TupleType(list(args), self.chk.named_type("tuple"))],
                        [ARG_POS],
                        [None],
                        TupleType(list(args), self.chk.named_type("tuple")),
                        tp.fallback,
                        name="tuple",
                        definition=tp.definition,
                        is_bound=tp.is_bound,
                    )
                self.msg.incompatible_type_application(
                    min_arg_count, len(type_vars), len(args), ctx
                )
                return AnyType(TypeOfAny.from_error)
            return self.apply_generic_arguments(tp, self.split_for_callable(tp, args, ctx), ctx)
        if isinstance(tp, Overloaded):
            for it in tp.items:
                if tp.is_type_obj():
                    # Same as above.
                    type_vars = tp.type_object().defn.type_vars
                else:
                    type_vars = list(it.variables)
                min_arg_count = sum(not v.has_default() for v in type_vars)
                has_type_var_tuple = any(isinstance(v, TypeVarTupleType) for v in type_vars)
                if (
                    len(args) < min_arg_count or len(args) > len(type_vars)
                ) and not has_type_var_tuple:
                    self.msg.incompatible_type_application(
                        min_arg_count, len(type_vars), len(args), ctx
                    )
                    return AnyType(TypeOfAny.from_error)
            return Overloaded(
                [
                    self.apply_generic_arguments(it, self.split_for_callable(it, args, ctx), ctx)
                    for it in tp.items
                ]
            )
        return AnyType(TypeOfAny.special_form)

    def visit_list_expr(self, e: ListExpr) -> Type:
        """Type check a list expression [...]."""
        return self.check_lst_expr(e, "builtins.list", "<list>")

    def visit_set_expr(self, e: SetExpr) -> Type:
        return self.check_lst_expr(e, "builtins.set", "<set>")

    def fast_container_type(
        self, e: ListExpr | SetExpr | TupleExpr, container_fullname: str
    ) -> Type | None:
        """
        Fast path to determine the type of a list or set literal,
        based on the list of entries. This mostly impacts large
        module-level constant definitions.

        Limitations:

         - no active type context
         - at least one item
         - no star expressions
         - not after deferral
         - either exactly one distinct type inside,
           or the joined type of all entries is an Instance or Tuple type,
        """
        ctx = self.type_context[-1]
        if ctx or not e.items:
            return None
        if self.chk.current_node_deferred:
            # Guarantees that all items will be Any, we'll reject it anyway.
            return None
        rt = self.resolved_type.get(e, None)
        if rt is not None:
            return rt if isinstance(rt, Instance) else None
        values: list[Type] = []
        # Preserve join order while avoiding O(n) lookups at every iteration
        values_set: set[Type] = set()
        for item in e.items:
            if isinstance(item, StarExpr):
                # fallback to slow path
                self.resolved_type[e] = NoneType()
                return None

            typ = self.accept(item)
            if typ not in values_set:
                values.append(typ)
                values_set.add(typ)

        vt = self._first_or_join_fast_item(values)
        if vt is None:
            self.resolved_type[e] = NoneType()
            return None
        ct = self.chk.named_generic_type(container_fullname, [vt])
        self.resolved_type[e] = ct
        return ct

    def _first_or_join_fast_item(self, items: list[Type]) -> Type | None:
        if len(items) == 1 and not self.chk.current_node_deferred:
            return items[0]
        typ = join.join_type_list(items)
        if not allow_fast_container_literal(typ):
            # TODO: This is overly strict, many other types can be joined safely here.
            # However, our join implementation isn't bug-free, and some joins may produce
            # undesired `Any`s or even more surprising results.
            return None
        return typ

    def check_lst_expr(self, e: ListExpr | SetExpr | TupleExpr, fullname: str, tag: str) -> Type:
        # fast path
        t = self.fast_container_type(e, fullname)
        if t:
            return t

        # Translate into type checking a generic function call.
        # Used for list and set expressions, as well as for tuples
        # containing star expressions that don't refer to a
        # Tuple. (Note: "lst" stands for list-set-tuple. :-)
        tv = TypeVarType(
            "T",
            "T",
            id=TypeVarId(-1, namespace="<lst>"),
            values=[],
            upper_bound=self.object_type(),
            default=AnyType(TypeOfAny.from_omitted_generics),
        )
        constructor = CallableType(
            [tv],
            [nodes.ARG_STAR],
            [None],
            self.chk.named_generic_type(fullname, [tv]),
            self.named_type("builtins.function"),
            name=tag,
            variables=[tv],
        )
        out = self.check_call(
            constructor,
            [(i.expr if isinstance(i, StarExpr) else i) for i in e.items],
            [(nodes.ARG_STAR if isinstance(i, StarExpr) else nodes.ARG_POS) for i in e.items],
            e,
        )[0]
        return remove_instance_last_known_values(out)

    def tuple_context_matches(self, expr: TupleExpr, ctx: TupleType) -> bool:
        ctx_unpack_index = find_unpack_in_list(ctx.items)
        if ctx_unpack_index is None:
            # For fixed tuples accept everything that can possibly match, even if this
            # requires all star items to be empty.
            return len([e for e in expr.items if not isinstance(e, StarExpr)]) <= len(ctx.items)
        # For variadic context, the only easy case is when structure matches exactly.
        # TODO: try using tuple type context in more cases.
        if len([e for e in expr.items if isinstance(e, StarExpr)]) != 1:
            return False
        expr_star_index = next(i for i, lv in enumerate(expr.items) if isinstance(lv, StarExpr))
        return len(expr.items) == len(ctx.items) and ctx_unpack_index == expr_star_index

    def visit_tuple_expr(self, e: TupleExpr) -> Type:
        """Type check a tuple expression."""
        # Try to determine type context for type inference.
        type_context = get_proper_type(self.type_context[-1])
        type_context_items = None
        if isinstance(type_context, UnionType):
            tuples_in_context = [
                t
                for t in get_proper_types(type_context.items)
                if (isinstance(t, TupleType) and self.tuple_context_matches(e, t))
                or is_named_instance(t, TUPLE_LIKE_INSTANCE_NAMES)
            ]
            if len(tuples_in_context) == 1:
                type_context = tuples_in_context[0]
            else:
                # There are either no relevant tuples in the Union, or there is
                # more than one.  Either way, we can't decide on a context.
                pass

        if isinstance(type_context, TupleType) and self.tuple_context_matches(e, type_context):
            type_context_items = type_context.items
        elif type_context and is_named_instance(type_context, TUPLE_LIKE_INSTANCE_NAMES):
            assert isinstance(type_context, Instance)
            if type_context.args:
                type_context_items = [type_context.args[0]] * len(e.items)
        # NOTE: it's possible for the context to have a different
        # number of items than e.  In that case we use those context
        # items that match a position in e, and we'll worry about type
        # mismatches later.

        unpack_in_context = False
        if type_context_items is not None:
            unpack_in_context = find_unpack_in_list(type_context_items) is not None
        seen_unpack_in_items = False
        allow_precise_tuples = (
            unpack_in_context or PRECISE_TUPLE_TYPES in self.chk.options.enable_incomplete_feature
        )

        # Infer item types.  Give up if there's a star expression
        # that's not a Tuple.
        items: list[Type] = []
        j = 0  # Index into type_context_items; irrelevant if type_context_items is none
        for i in range(len(e.items)):
            item = e.items[i]
            if isinstance(item, StarExpr):
                # Special handling for star expressions.
                # TODO: If there's a context, and item.expr is a
                # TupleExpr, flatten it, so we can benefit from the
                # context?  Counterargument: Why would anyone write
                # (1, *(2, 3)) instead of (1, 2, 3) except in a test?
                if unpack_in_context:
                    # Note: this logic depends on full structure match in tuple_context_matches().
                    assert type_context_items
                    ctx_item = type_context_items[j]
                    assert isinstance(ctx_item, UnpackType)
                    ctx = ctx_item.type
                else:
                    ctx = None
                tt = self.accept(item.expr, ctx)
                tt = get_proper_type(tt)
                if isinstance(tt, TupleType):
                    if find_unpack_in_list(tt.items) is not None:
                        if seen_unpack_in_items:
                            # Multiple unpack items are not allowed in tuples,
                            # fall back to instance type.
                            return self.check_lst_expr(e, "builtins.tuple", "<tuple>")
                        else:
                            seen_unpack_in_items = True
                    items.extend(tt.items)
                    # Note: this logic depends on full structure match in tuple_context_matches().
                    if unpack_in_context:
                        j += 1
                    else:
                        # If there is an unpack in expressions, but not in context, this will
                        # result in an error later, just do something predictable here.
                        j += len(tt.items)
                else:
                    if allow_precise_tuples and not seen_unpack_in_items:
                        # Handle (x, *y, z), where y is e.g. tuple[Y, ...].
                        if isinstance(tt, Instance) and self.chk.type_is_iterable(tt):
                            item_type = self.chk.iterable_item_type(tt, e)
                            mapped = self.chk.named_generic_type("builtins.tuple", [item_type])
                            items.append(UnpackType(mapped))
                            seen_unpack_in_items = True
                            continue
                    # A star expression that's not a Tuple.
                    # Treat the whole thing as a variable-length tuple.
                    return self.check_lst_expr(e, "builtins.tuple", "<tuple>")
            else:
                if not type_context_items or j >= len(type_context_items):
                    tt = self.accept(item)
                else:
                    tt = self.accept(item, type_context_items[j])
                    j += 1
                items.append(tt)
        # This is a partial fallback item type. A precise type will be calculated on demand.
        fallback_item = AnyType(TypeOfAny.special_form)
        result: ProperType = TupleType(
            items, self.chk.named_generic_type("builtins.tuple", [fallback_item])
        )
        if seen_unpack_in_items:
            # Return already normalized tuple type just in case.
            result = expand_type(result, {})
        return result

    def fast_dict_type(self, e: DictExpr) -> Type | None:
        """
        Fast path to determine the type of a dict literal,
        based on the list of entries. This mostly impacts large
        module-level constant definitions.

        Limitations:

         - no active type context
         - at least one item
         - only supported star expressions are other dict instances
         - either exactly one distinct type (keys and values separately) inside,
           or the joined type of all entries is an Instance or Tuple type
        """
        ctx = self.type_context[-1]
        if ctx or not e.items:
            return None

        if self.chk.current_node_deferred:
            # Guarantees that all items will be Any, we'll reject it anyway.
            return None

        rt = self.resolved_type.get(e, None)
        if rt is not None:
            return rt if isinstance(rt, Instance) else None

        keys: list[Type] = []
        values: list[Type] = []
        # Preserve join order while avoiding O(n) lookups at every iteration
        keys_set: set[Type] = set()
        values_set: set[Type] = set()
        stargs: tuple[Type, Type] | None = None
        for key, value in e.items:
            if key is None:
                st = get_proper_type(self.accept(value))
                if (
                    isinstance(st, Instance)
                    and st.type.fullname == "builtins.dict"
                    and len(st.args) == 2
                ):
                    stargs = (st.args[0], st.args[1])
                else:
                    self.resolved_type[e] = NoneType()
                    return None
            else:
                key_t = self.accept(key)
                if key_t not in keys_set:
                    keys.append(key_t)
                    keys_set.add(key_t)
                value_t = self.accept(value)
                if value_t not in values_set:
                    values.append(value_t)
                    values_set.add(value_t)

        kt = self._first_or_join_fast_item(keys)
        if kt is None:
            self.resolved_type[e] = NoneType()
            return None

        vt = self._first_or_join_fast_item(values)
        if vt is None:
            self.resolved_type[e] = NoneType()
            return None

        if stargs and (stargs[0] != kt or stargs[1] != vt):
            self.resolved_type[e] = NoneType()
            return None
        dt = self.chk.named_generic_type("builtins.dict", [kt, vt])
        self.resolved_type[e] = dt
        return dt

    def check_typeddict_literal_in_context(
        self, e: DictExpr, typeddict_context: TypedDictType
    ) -> Type:
        orig_ret_type = self.check_typeddict_call_with_dict(
            callee=typeddict_context, kwargs=e.items, context=e, orig_callee=None
        )
        ret_type = get_proper_type(orig_ret_type)
        if isinstance(ret_type, TypedDictType):
            return ret_type.copy_modified()
        return typeddict_context.copy_modified()

    def visit_dict_expr(self, e: DictExpr) -> Type:
        """Type check a dict expression.

        Translate it into a call to dict(), with provisions for **expr.
        """
        # if the dict literal doesn't match TypedDict, check_typeddict_call_with_dict reports
        # an error, but returns the TypedDict type that matches the literal it found
        # that would cause a second error when that TypedDict type is returned upstream
        # to avoid the second error, we always return TypedDict type that was requested
        typeddict_contexts = self.find_typeddict_context(self.type_context[-1], e)
        if typeddict_contexts:
            if len(typeddict_contexts) == 1:
                return self.check_typeddict_literal_in_context(e, typeddict_contexts[0])
            # Multiple items union, check if at least one of them matches cleanly.
            for typeddict_context in typeddict_contexts:
                with self.msg.filter_errors() as err, self.chk.local_type_map() as tmap:
                    ret_type = self.check_typeddict_literal_in_context(e, typeddict_context)
                if err.has_new_errors():
                    continue
                self.chk.store_types(tmap)
                return ret_type
            # No item matched without an error, so we can't unambiguously choose the item.
            self.msg.typeddict_context_ambiguous(typeddict_contexts, e)

        # fast path attempt
        dt = self.fast_dict_type(e)
        if dt:
            return dt

        # Define type variables (used in constructors below).
        kt = TypeVarType(
            "KT",
            "KT",
            id=TypeVarId(-1, namespace="<dict>"),
            values=[],
            upper_bound=self.object_type(),
            default=AnyType(TypeOfAny.from_omitted_generics),
        )
        vt = TypeVarType(
            "VT",
            "VT",
            id=TypeVarId(-2, namespace="<dict>"),
            values=[],
            upper_bound=self.object_type(),
            default=AnyType(TypeOfAny.from_omitted_generics),
        )

        # Collect function arguments, watching out for **expr.
        args: list[Expression] = []
        expected_types: list[Type] = []
        for key, value in e.items:
            if key is None:
                args.append(value)
                expected_types.append(
                    self.chk.named_generic_type("_typeshed.SupportsKeysAndGetItem", [kt, vt])
                )
            else:
                tup = TupleExpr([key, value])
                if key.line >= 0:
                    tup.line = key.line
                    tup.column = key.column
                else:
                    tup.line = value.line
                    tup.column = value.column
                tup.end_line = value.end_line
                tup.end_column = value.end_column
                args.append(tup)
                expected_types.append(TupleType([kt, vt], self.named_type("builtins.tuple")))

        # The callable type represents a function like this (except we adjust for **expr):
        #   def <dict>(*v: Tuple[kt, vt]) -> Dict[kt, vt]: ...
        constructor = CallableType(
            expected_types,
            [nodes.ARG_POS] * len(expected_types),
            [None] * len(expected_types),
            self.chk.named_generic_type("builtins.dict", [kt, vt]),
            self.named_type("builtins.function"),
            name="<dict>",
            variables=[kt, vt],
        )
        return self.check_call(constructor, args, [nodes.ARG_POS] * len(args), e)[0]

    def find_typeddict_context(
        self, context: Type | None, dict_expr: DictExpr
    ) -> list[TypedDictType]:
        context = get_proper_type(context)
        if isinstance(context, TypedDictType):
            return [context]
        elif isinstance(context, UnionType):
            items = []
            for item in context.items:
                item_contexts = self.find_typeddict_context(item, dict_expr)
                for item_context in item_contexts:
                    if self.match_typeddict_call_with_dict(
                        item_context, dict_expr.items, dict_expr
                    ):
                        items.append(item_context)
            return items
        # No TypedDict type in context.
        return []

    def visit_lambda_expr(self, e: LambdaExpr) -> Type:
        """Type check lambda expression."""
        old_in_lambda = self.in_lambda_expr
        self.in_lambda_expr = True
        self.chk.check_default_args(e, body_is_trivial=False)
        inferred_type, type_override = self.infer_lambda_type_using_context(e)
        if not inferred_type:
            self.chk.return_types.append(AnyType(TypeOfAny.special_form))
            # Type check everything in the body except for the final return
            # statement (it can contain tuple unpacking before return).
            with (
                self.chk.binder.frame_context(can_skip=True, fall_through=0),
                self.chk.scope.push_function(e),
            ):
                # Lambdas can have more than one element in body,
                # when we add "fictional" AssignmentStatement nodes, like in:
                # `lambda (a, b): a`
                for stmt in e.body.body[:-1]:
                    stmt.accept(self.chk)
                # Only type check the return expression, not the return statement.
                # There's no useful type context.
                ret_type = self.accept(e.expr(), allow_none_return=True)
            fallback = self.named_type("builtins.function")
            self.chk.return_types.pop()
            self.in_lambda_expr = old_in_lambda
            return callable_type(e, fallback, ret_type)
        else:
            # Type context available.
            self.chk.return_types.append(inferred_type.ret_type)
            with self.chk.tscope.function_scope(e):
                self.chk.check_func_item(e, type_override=type_override)
            if not self.chk.has_type(e.expr()):
                # TODO: return expression must be accepted before exiting function scope.
                with self.chk.binder.frame_context(can_skip=True, fall_through=0):
                    self.accept(e.expr(), allow_none_return=True)
            ret_type = self.chk.lookup_type(e.expr())
            self.chk.return_types.pop()
            self.in_lambda_expr = old_in_lambda
            return replace_callable_return_type(inferred_type, ret_type)

    def infer_lambda_type_using_context(
        self, e: LambdaExpr
    ) -> tuple[CallableType | None, CallableType | None]:
        """Try to infer lambda expression type using context.

        Return None if could not infer type.
        The second item in the return type is the type_override parameter for check_func_item.
        """
        # TODO also accept 'Any' context
        ctx = get_proper_type(self.type_context[-1])

        if isinstance(ctx, UnionType):
            callables = [
                t for t in get_proper_types(ctx.relevant_items()) if isinstance(t, CallableType)
            ]
            if len(callables) == 1:
                ctx = callables[0]

        if not ctx or not isinstance(ctx, CallableType):
            return None, None

        # The context may have function type variables in it. We replace them
        # since these are the type variables we are ultimately trying to infer;
        # they must be considered as indeterminate. We use ErasedType since it
        # does not affect type inference results (it is for purposes like this
        # only).
        if not self.chk.options.old_type_inference:
            # With new type inference we can preserve argument types even if they
            # are generic, since new inference algorithm can handle constraints
            # like S <: T (we still erase return type since it's ultimately unknown).
            extra_vars = []
            for arg in ctx.arg_types:
                meta_vars = [tv for tv in get_all_type_vars(arg) if tv.id.is_meta_var()]
                extra_vars.extend([tv for tv in meta_vars if tv not in extra_vars])
            callable_ctx = ctx.copy_modified(
                ret_type=replace_meta_vars(ctx.ret_type, ErasedType()),
                variables=list(ctx.variables) + extra_vars,
            )
        else:
            erased_ctx = replace_meta_vars(ctx, ErasedType())
            assert isinstance(erased_ctx, ProperType) and isinstance(erased_ctx, CallableType)
            callable_ctx = erased_ctx

        # The callable_ctx may have a fallback of builtins.type if the context
        # is a constructor -- but this fallback doesn't make sense for lambdas.
        callable_ctx = callable_ctx.copy_modified(fallback=self.named_type("builtins.function"))

        if callable_ctx.type_guard is not None or callable_ctx.type_is is not None:
            # Lambda's return type cannot be treated as a `TypeGuard`,
            # because it is implicit. And `TypeGuard`s must be explicit.
            # See https://github.com/python/mypy/issues/9927
            return None, None

        arg_kinds = [arg.kind for arg in e.arguments]

        if callable_ctx.is_ellipsis_args or ctx.param_spec() is not None:
            # Fill in Any arguments to match the arguments of the lambda.
            callable_ctx = callable_ctx.copy_modified(
                is_ellipsis_args=False,
                arg_types=[AnyType(TypeOfAny.special_form)] * len(arg_kinds),
                arg_kinds=arg_kinds,
                arg_names=e.arg_names.copy(),
            )

        if ARG_STAR in arg_kinds or ARG_STAR2 in arg_kinds:
            # TODO treat this case appropriately
            return callable_ctx, None

        if callable_ctx.arg_kinds != arg_kinds:
            # Incompatible context; cannot use it to infer types.
            self.chk.fail(message_registry.CANNOT_INFER_LAMBDA_TYPE, e)
            return None, None

        # Type of lambda must have correct argument names, to prevent false
        # negatives when lambdas appear in `ParamSpec` context.
        return callable_ctx.copy_modified(arg_names=e.arg_names), callable_ctx

    def visit_super_expr(self, e: SuperExpr) -> Type:
        """Type check a super expression (non-lvalue)."""

        # We have an expression like super(T, var).member

        # First compute the types of T and var
        types = self._super_arg_types(e)
        if isinstance(types, tuple):
            type_type, instance_type = types
        else:
            return types

        # Now get the MRO
        type_info = type_info_from_type(type_type)
        if type_info is None:
            self.chk.fail(message_registry.UNSUPPORTED_ARG_1_FOR_SUPER, e)
            return AnyType(TypeOfAny.from_error)

        instance_info = type_info_from_type(instance_type)
        if instance_info is None:
            self.chk.fail(message_registry.UNSUPPORTED_ARG_2_FOR_SUPER, e)
            return AnyType(TypeOfAny.from_error)

        mro = instance_info.mro

        # The base is the first MRO entry *after* type_info that has a member
        # with the right name
        index = None
        if type_info in mro:
            index = mro.index(type_info)
        else:
            method = self.chk.scope.current_function()
            # Mypy explicitly allows supertype upper bounds (and no upper bound at all)
            # for annotating self-types. However, if such an annotation is used for
            # checking super() we will still get an error. So to be consistent, we also
            # allow such imprecise annotations for use with super(), where we fall back
            # to the current class MRO instead. This works only from inside a method.
            if method is not None and is_self_type_like(
                instance_type, is_classmethod=method.is_class
            ):
                if e.info and type_info in e.info.mro:
                    mro = e.info.mro
                    index = mro.index(type_info)
        if index is None:
            if (
                instance_info.is_protocol
                and instance_info != type_info
                and not type_info.is_protocol
            ):
                # A special case for mixins, in this case super() should point
                # directly to the host protocol, this is not safe, since the real MRO
                # is not known yet for mixin, but this feature is more like an escape hatch.
                index = -1
            else:
                self.chk.fail(message_registry.SUPER_ARG_2_NOT_INSTANCE_OF_ARG_1, e)
                return AnyType(TypeOfAny.from_error)

        if len(mro) == index + 1:
            self.chk.fail(message_registry.TARGET_CLASS_HAS_NO_BASE_CLASS, e)
            return AnyType(TypeOfAny.from_error)

        for base in mro[index + 1 :]:
            if e.name in base.names or base == mro[-1]:
                if e.info and e.info.fallback_to_any and base == mro[-1]:
                    # There's an undefined base class, and we're at the end of the
                    # chain.  That's not an error.
                    return AnyType(TypeOfAny.special_form)

                return analyze_member_access(
                    name=e.name,
                    typ=instance_type,
                    is_lvalue=False,
                    is_super=True,
                    is_operator=False,
                    original_type=instance_type,
                    override_info=base,
                    context=e,
                    chk=self.chk,
                    in_literal_context=self.is_literal_context(),
                )

        assert False, "unreachable"

    def _super_arg_types(self, e: SuperExpr) -> Type | tuple[Type, Type]:
        """
        Computes the types of the type and instance expressions in super(T, instance), or the
        implicit ones for zero-argument super() expressions.  Returns a single type for the whole
        super expression when possible (for errors, anys), otherwise the pair of computed types.
        """

        if not self.chk.in_checked_function():
            return AnyType(TypeOfAny.unannotated)
        elif len(e.call.args) == 0:
            if not e.info:
                # This has already been reported by the semantic analyzer.
                return AnyType(TypeOfAny.from_error)
            elif self.chk.scope.active_class():
                self.chk.fail(message_registry.SUPER_OUTSIDE_OF_METHOD_NOT_SUPPORTED, e)
                return AnyType(TypeOfAny.from_error)

            # Zero-argument super() is like super(<current class>, <self>)
            current_type = fill_typevars(e.info)
            type_type: ProperType = TypeType(current_type)

            # Use the type of the self argument, in case it was annotated
            method = self.chk.scope.current_function()
            assert method is not None
            if method.arguments:
                instance_type: Type = method.arguments[0].variable.type or current_type
            else:
                self.chk.fail(message_registry.SUPER_ENCLOSING_POSITIONAL_ARGS_REQUIRED, e)
                return AnyType(TypeOfAny.from_error)
        elif ARG_STAR in e.call.arg_kinds:
            self.chk.fail(message_registry.SUPER_VARARGS_NOT_SUPPORTED, e)
            return AnyType(TypeOfAny.from_error)
        elif set(e.call.arg_kinds) != {ARG_POS}:
            self.chk.fail(message_registry.SUPER_POSITIONAL_ARGS_REQUIRED, e)
            return AnyType(TypeOfAny.from_error)
        elif len(e.call.args) == 1:
            self.chk.fail(message_registry.SUPER_WITH_SINGLE_ARG_NOT_SUPPORTED, e)
            return AnyType(TypeOfAny.from_error)
        elif len(e.call.args) == 2:
            type_type = get_proper_type(self.accept(e.call.args[0]))
            instance_type = self.accept(e.call.args[1])
        else:
            self.chk.fail(message_registry.TOO_MANY_ARGS_FOR_SUPER, e)
            return AnyType(TypeOfAny.from_error)

        # Imprecisely assume that the type is the current class
        if isinstance(type_type, AnyType):
            if e.info:
                type_type = TypeType(fill_typevars(e.info))
            else:
                return AnyType(TypeOfAny.from_another_any, source_any=type_type)
        elif isinstance(type_type, TypeType):
            type_item = type_type.item
            if isinstance(type_item, AnyType):
                if e.info:
                    type_type = TypeType(fill_typevars(e.info))
                else:
                    return AnyType(TypeOfAny.from_another_any, source_any=type_item)

        if not isinstance(type_type, TypeType) and not (
            isinstance(type_type, FunctionLike) and type_type.is_type_obj()
        ):
            self.msg.first_argument_for_super_must_be_type(type_type, e)
            return AnyType(TypeOfAny.from_error)

        # Imprecisely assume that the instance is of the current class
        instance_type = get_proper_type(instance_type)
        if isinstance(instance_type, AnyType):
            if e.info:
                instance_type = fill_typevars(e.info)
            else:
                return AnyType(TypeOfAny.from_another_any, source_any=instance_type)
        elif isinstance(instance_type, TypeType):
            instance_item = instance_type.item
            if isinstance(instance_item, AnyType):
                if e.info:
                    instance_type = TypeType(fill_typevars(e.info))
                else:
                    return AnyType(TypeOfAny.from_another_any, source_any=instance_item)

        return type_type, instance_type

    def visit_slice_expr(self, e: SliceExpr) -> Type:
        try:
            supports_index = self.chk.named_type("typing_extensions.SupportsIndex")
        except KeyError:
            supports_index = self.chk.named_type("builtins.int")  # thanks, fixture life
        expected = make_optional_type(supports_index)
        type_args = []
        for index in [e.begin_index, e.end_index, e.stride]:
            if index:
                t = self.accept(index)
                self.chk.check_subtype(t, expected, index, message_registry.INVALID_SLICE_INDEX)
                type_args.append(t)
            else:
                type_args.append(NoneType())
        return self.chk.named_generic_type("builtins.slice", type_args)

    def visit_list_comprehension(self, e: ListComprehension) -> Type:
        return self.check_generator_or_comprehension(
            e.generator, "builtins.list", "<list-comprehension>"
        )

    def visit_set_comprehension(self, e: SetComprehension) -> Type:
        return self.check_generator_or_comprehension(
            e.generator, "builtins.set", "<set-comprehension>"
        )

    def visit_generator_expr(self, e: GeneratorExpr) -> Type:
        # If any of the comprehensions use async for, the expression will return an async generator
        # object, or await is used anywhere but in the leftmost sequence.
        if (
            any(e.is_async)
            or has_await_expression(e.left_expr)
            or any(has_await_expression(sequence) for sequence in e.sequences[1:])
            or any(has_await_expression(cond) for condlist in e.condlists for cond in condlist)
        ):
            typ = "typing.AsyncGenerator"
            # received type is always None in async generator expressions
            additional_args: list[Type] = [NoneType()]
        else:
            typ = "typing.Generator"
            # received type and returned type are None
            additional_args = [NoneType(), NoneType()]
        return self.check_generator_or_comprehension(
            e, typ, "<generator>", additional_args=additional_args
        )

    def check_generator_or_comprehension(
        self,
        gen: GeneratorExpr,
        type_name: str,
        id_for_messages: str,
        additional_args: list[Type] | None = None,
    ) -> Type:
        """Type check a generator expression or a list comprehension."""
        additional_args = additional_args or []
        with self.chk.binder.frame_context(can_skip=True, fall_through=0):
            self.check_for_comp(gen)

            # Infer the type of the list comprehension by using a synthetic generic
            # callable type.
            tv = TypeVarType(
                "T",
                "T",
                id=TypeVarId(-1, namespace="<genexp>"),
                values=[],
                upper_bound=self.object_type(),
                default=AnyType(TypeOfAny.from_omitted_generics),
            )
            tv_list: list[Type] = [tv]
            constructor = CallableType(
                tv_list,
                [nodes.ARG_POS],
                [None],
                self.chk.named_generic_type(type_name, tv_list + additional_args),
                self.chk.named_type("builtins.function"),
                name=id_for_messages,
                variables=[tv],
            )
            return self.check_call(constructor, [gen.left_expr], [nodes.ARG_POS], gen)[0]

    def visit_dictionary_comprehension(self, e: DictionaryComprehension) -> Type:
        """Type check a dictionary comprehension."""
        with self.chk.binder.frame_context(can_skip=True, fall_through=0):
            self.check_for_comp(e)

            # Infer the type of the list comprehension by using a synthetic generic
            # callable type.
            ktdef = TypeVarType(
                "KT",
                "KT",
                id=TypeVarId(-1, namespace="<dict>"),
                values=[],
                upper_bound=self.object_type(),
                default=AnyType(TypeOfAny.from_omitted_generics),
            )
            vtdef = TypeVarType(
                "VT",
                "VT",
                id=TypeVarId(-2, namespace="<dict>"),
                values=[],
                upper_bound=self.object_type(),
                default=AnyType(TypeOfAny.from_omitted_generics),
            )
            constructor = CallableType(
                [ktdef, vtdef],
                [nodes.ARG_POS, nodes.ARG_POS],
                [None, None],
                self.chk.named_generic_type("builtins.dict", [ktdef, vtdef]),
                self.chk.named_type("builtins.function"),
                name="<dictionary-comprehension>",
                variables=[ktdef, vtdef],
            )
            return self.check_call(
                constructor, [e.key, e.value], [nodes.ARG_POS, nodes.ARG_POS], e
            )[0]

    def check_for_comp(self, e: GeneratorExpr | DictionaryComprehension) -> None:
        """Check the for_comp part of comprehensions. That is the part from 'for':
        ... for x in y if z

        Note: This adds the type information derived from the condlists to the current binder.
        """
        for index, sequence, conditions, is_async in zip(
            e.indices, e.sequences, e.condlists, e.is_async
        ):
            if is_async:
                _, sequence_type = self.chk.analyze_async_iterable_item_type(sequence)
            else:
                _, sequence_type = self.chk.analyze_iterable_item_type(sequence)
                if (
                    isinstance(get_proper_type(sequence_type), UninhabitedType)
                    and isinstance(index, NameExpr)
                    and index.name == "_"
                ):
                    # To preserve backward compatibility, avoid inferring Never for "_"
                    sequence_type = AnyType(TypeOfAny.special_form)

            self.chk.analyze_index_variables(index, sequence_type, True, e)
            for condition in conditions:
                self.accept(condition)

                # values are only part of the comprehension when all conditions are true
                true_map, false_map = self.chk.find_isinstance_check(condition)

                if true_map:
                    self.chk.push_type_map(true_map)

                if codes.REDUNDANT_EXPR in self.chk.options.enabled_error_codes:
                    if true_map is None:
                        self.msg.redundant_condition_in_comprehension(False, condition)
                    elif false_map is None:
                        self.msg.redundant_condition_in_comprehension(True, condition)

    def visit_conditional_expr(self, e: ConditionalExpr, allow_none_return: bool = False) -> Type:
        self.accept(e.cond)
        ctx = self.type_context[-1]

        # Gain type information from isinstance if it is there
        # but only for the current expression
        if_map, else_map = self.chk.find_isinstance_check(e.cond)
        if codes.REDUNDANT_EXPR in self.chk.options.enabled_error_codes:
            if if_map is None:
                self.msg.redundant_condition_in_if(False, e.cond)
            elif else_map is None:
                self.msg.redundant_condition_in_if(True, e.cond)

        if_type = self.analyze_cond_branch(
            if_map, e.if_expr, context=ctx, allow_none_return=allow_none_return
        )

        # we want to keep the narrowest value of if_type for union'ing the branches
        # however, it would be silly to pass a literal as a type context. Pass the
        # underlying fallback type instead.
        if_type_fallback = simple_literal_type(get_proper_type(if_type)) or if_type

        # Analyze the right branch using full type context and store the type
        full_context_else_type = self.analyze_cond_branch(
            else_map, e.else_expr, context=ctx, allow_none_return=allow_none_return
        )

        if not mypy.checker.is_valid_inferred_type(if_type, self.chk.options):
            # Analyze the right branch disregarding the left branch.
            else_type = full_context_else_type
            # we want to keep the narrowest value of else_type for union'ing the branches
            # however, it would be silly to pass a literal as a type context. Pass the
            # underlying fallback type instead.
            else_type_fallback = simple_literal_type(get_proper_type(else_type)) or else_type

            # If it would make a difference, re-analyze the left
            # branch using the right branch's type as context.
            if ctx is None or not is_equivalent(else_type_fallback, ctx):
                # TODO: If it's possible that the previous analysis of
                # the left branch produced errors that are avoided
                # using this context, suppress those errors.
                if_type = self.analyze_cond_branch(
                    if_map,
                    e.if_expr,
                    context=else_type_fallback,
                    allow_none_return=allow_none_return,
                )

        elif if_type_fallback == ctx:
            # There is no point re-running the analysis if if_type is equal to ctx.
            # That would  be an exact duplicate of the work we just did.
            # This optimization is particularly important to avoid exponential blowup with nested
            # if/else expressions: https://github.com/python/mypy/issues/9591
            # TODO: would checking for is_proper_subtype also work and cover more cases?
            else_type = full_context_else_type
        else:
            # Analyze the right branch in the context of the left
            # branch's type.
            else_type = self.analyze_cond_branch(
                else_map,
                e.else_expr,
                context=if_type_fallback,
                allow_none_return=allow_none_return,
            )

        # In most cases using if_type as a context for right branch gives better inferred types.
        # This is however not the case for literal types, so use the full context instead.
        if is_literal_type_like(full_context_else_type) and not is_literal_type_like(else_type):
            else_type = full_context_else_type

        res: Type = make_simplified_union([if_type, else_type])
        if has_uninhabited_component(res) and not isinstance(
            get_proper_type(self.type_context[-1]), UnionType
        ):
            # In rare cases with empty collections join may give a better result.
            alternative = join.join_types(if_type, else_type)
            p_alt = get_proper_type(alternative)
            if not isinstance(p_alt, Instance) or p_alt.type.fullname != "builtins.object":
                res = alternative
        return res

    def analyze_cond_branch(
        self,
        map: dict[Expression, Type] | None,
        node: Expression,
        context: Type | None,
        allow_none_return: bool = False,
        suppress_unreachable_errors: bool = True,
    ) -> Type:
        with self.chk.binder.frame_context(can_skip=True, fall_through=0):
            if map is None:
                # We still need to type check node, in case we want to
                # process it for isinstance checks later. Since the branch was
                # determined to be unreachable, any errors should be suppressed.
                with self.msg.filter_errors(filter_errors=suppress_unreachable_errors):
                    self.accept(node, type_context=context, allow_none_return=allow_none_return)
                return UninhabitedType()
            self.chk.push_type_map(map)
            return self.accept(node, type_context=context, allow_none_return=allow_none_return)

    def _combined_context(self, ty: Type | None) -> Type | None:
        ctx_items = []
        if ty is not None:
            ctx_items.append(ty)
        if self.type_context and self.type_context[-1] is not None:
            ctx_items.append(self.type_context[-1])
        if ctx_items:
            return make_simplified_union(ctx_items)
        return None

    #
    # Helpers
    #

    def accept(
        self,
        node: Expression,
        type_context: Type | None = None,
        allow_none_return: bool = False,
        always_allow_any: bool = False,
        is_callee: bool = False,
    ) -> Type:
        """Type check a node in the given type context.  If allow_none_return
        is True and this expression is a call, allow it to return None.  This
        applies only to this expression and not any subexpressions.
        """
        if node in self.type_overrides:
            # This branch is very fast, there is no point timing it.
            return self.type_overrides[node]
        # We don't use context manager here to get most precise data (and avoid overhead).
        record_time = False
        if self.collect_line_checking_stats and not self.in_expression:
            t0 = time.perf_counter_ns()
            self.in_expression = True
            record_time = True
        self.type_context.append(type_context)
        old_is_callee = self.is_callee
        self.is_callee = is_callee
        try:
            if allow_none_return and isinstance(node, CallExpr):
                typ = self.visit_call_expr(node, allow_none_return=True)
            elif allow_none_return and isinstance(node, YieldFromExpr):
                typ = self.visit_yield_from_expr(node, allow_none_return=True)
            elif allow_none_return and isinstance(node, ConditionalExpr):
                typ = self.visit_conditional_expr(node, allow_none_return=True)
            elif allow_none_return and isinstance(node, AwaitExpr):
                typ = self.visit_await_expr(node, allow_none_return=True)
            # Deeply nested generic calls can deteriorate performance dramatically.
            # Although in most cases caching makes little difference, in worst case
            # it avoids exponential complexity.
            # We cannot use cache inside lambdas, because they skip immediate type
            # context, and use enclosing one, see infer_lambda_type_using_context().
            # TODO: consider using cache for more expression kinds.
            elif isinstance(node, (CallExpr, ListExpr, TupleExpr, DictExpr, OpExpr)) and not (
                self.in_lambda_expr or self.chk.current_node_deferred
            ):
                if (node, type_context) in self.expr_cache:
                    binder_version, typ, messages, type_map = self.expr_cache[(node, type_context)]
                    if binder_version == self.chk.binder.version:
                        self.chk.store_types(type_map)
                        self.msg.add_errors(messages)
                    else:
                        typ = self.accept_maybe_cache(node, type_context=type_context)
                else:
                    typ = self.accept_maybe_cache(node, type_context=type_context)
            else:
                typ = node.accept(self)
        except Exception as err:
            report_internal_error(
                err, self.chk.errors.file, node.line, self.chk.errors, self.chk.options
            )
        self.is_callee = old_is_callee
        self.type_context.pop()
        assert typ is not None
        self.chk.store_type(node, typ)

        if (
            self.chk.options.disallow_any_expr
            and not always_allow_any
            and not self.chk.is_stub
            and self.chk.in_checked_function()
            and has_any_type(typ)
            and not self.chk.current_node_deferred
        ):
            self.msg.disallowed_any_type(typ, node)

        if not self.chk.in_checked_function() or self.chk.current_node_deferred:
            result: Type = AnyType(TypeOfAny.unannotated)
        else:
            result = typ
        if record_time:
            self.per_line_checking_time_ns[node.line] += time.perf_counter_ns() - t0
            self.in_expression = False
        return result

    def accept_maybe_cache(self, node: Expression, type_context: Type | None = None) -> Type:
        binder_version = self.chk.binder.version
        # Micro-optimization: inline local_type_map() as it is somewhat slow in mypyc.
        type_map: dict[Expression, Type] = {}
        self.chk._type_maps.append(type_map)
        with self.msg.filter_errors(filter_errors=True, save_filtered_errors=True) as msg:
            typ = node.accept(self)
        messages = msg.filtered_errors()
        if binder_version == self.chk.binder.version and not self.chk.current_node_deferred:
            self.expr_cache[(node, type_context)] = (binder_version, typ, messages, type_map)
        self.chk._type_maps.pop()
        self.chk.store_types(type_map)
        self.msg.add_errors(messages)
        return typ

    def named_type(self, name: str) -> Instance:
        """Return an instance type with type given by the name and no type
        arguments. Alias for TypeChecker.named_type.
        """
        return self.chk.named_type(name)

    def type_alias_type_type(self) -> Instance:
        """Returns a `typing.TypeAliasType` or `typing_extensions.TypeAliasType`."""
        if self.chk.options.python_version >= (3, 12):
            return self.named_type("typing.TypeAliasType")
        return self.named_type("typing_extensions.TypeAliasType")

    def is_valid_var_arg(self, typ: Type) -> bool:
        """Is a type valid as a *args argument?"""
        typ = get_proper_type(typ)
        return isinstance(typ, (TupleType, AnyType, ParamSpecType, UnpackType)) or is_subtype(
            typ, self.chk.named_generic_type("typing.Iterable", [AnyType(TypeOfAny.special_form)])
        )

    def is_valid_keyword_var_arg(self, typ: Type) -> bool:
        """Is a type valid as a **kwargs argument?"""
        return (
            is_subtype(
                typ,
                self.chk.named_generic_type(
                    "_typeshed.SupportsKeysAndGetItem",
                    [self.named_type("builtins.str"), AnyType(TypeOfAny.special_form)],
                ),
            )
            or is_subtype(
                typ,
                self.chk.named_generic_type(
                    "_typeshed.SupportsKeysAndGetItem", [UninhabitedType(), UninhabitedType()]
                ),
            )
            or isinstance(typ, ParamSpecType)
        )

    def not_ready_callback(self, name: str, context: Context) -> None:
        """Called when we can't infer the type of a variable because it's not ready yet.

        Either defer type checking of the enclosing function to the next
        pass or report an error.
        """
        self.chk.handle_cannot_determine_type(name, context)

    def visit_yield_expr(self, e: YieldExpr) -> Type:
        return_type = self.chk.return_types[-1]
        expected_item_type = self.chk.get_generator_yield_type(return_type, False)
        if e.expr is None:
            if (
                not isinstance(get_proper_type(expected_item_type), (NoneType, AnyType))
                and self.chk.in_checked_function()
            ):
                self.chk.fail(message_registry.YIELD_VALUE_EXPECTED, e)
        else:
            actual_item_type = self.accept(e.expr, expected_item_type)
            self.chk.check_subtype(
                actual_item_type,
                expected_item_type,
                e,
                message_registry.INCOMPATIBLE_TYPES_IN_YIELD,
                "actual type",
                "expected type",
            )
        return self.chk.get_generator_receive_type(return_type, False)

    def visit_await_expr(self, e: AwaitExpr, allow_none_return: bool = False) -> Type:
        expected_type = self.type_context[-1]
        if expected_type is not None:
            expected_type = self.chk.named_generic_type("typing.Awaitable", [expected_type])
        actual_type = get_proper_type(self.accept(e.expr, expected_type))
        if isinstance(actual_type, AnyType):
            return AnyType(TypeOfAny.from_another_any, source_any=actual_type)
        ret = self.check_awaitable_expr(
            actual_type, e, message_registry.INCOMPATIBLE_TYPES_IN_AWAIT
        )
        if not allow_none_return and isinstance(get_proper_type(ret), NoneType):
            self.chk.msg.does_not_return_value(None, e)
        return ret

    def check_awaitable_expr(
        self, t: Type, ctx: Context, msg: str | ErrorMessage, ignore_binder: bool = False
    ) -> Type:
        """Check the argument to `await` and extract the type of value.

        Also used by `async for` and `async with`.
        """
        if not self.chk.check_subtype(
            t, self.named_type("typing.Awaitable"), ctx, msg, "actual type", "expected type"
        ):
            return AnyType(TypeOfAny.special_form)
        else:
            generator = self.check_method_call_by_name("__await__", t, [], [], ctx)[0]
            ret_type = self.chk.get_generator_return_type(generator, False)
            ret_type = get_proper_type(ret_type)
            if (
                not ignore_binder
                and isinstance(ret_type, UninhabitedType)
                and not ret_type.ambiguous
            ):
                self.chk.binder.unreachable()
            return ret_type

    def visit_yield_from_expr(self, e: YieldFromExpr, allow_none_return: bool = False) -> Type:
        # NOTE: Whether `yield from` accepts an `async def` decorated
        # with `@types.coroutine` (or `@asyncio.coroutine`) depends on
        # whether the generator containing the `yield from` is itself
        # thus decorated.  But it accepts a generator regardless of
        # how it's decorated.
        return_type = self.chk.return_types[-1]
        # TODO: What should the context for the sub-expression be?
        # If the containing function has type Generator[X, Y, ...],
        # the context should be Generator[X, Y, T], where T is the
        # context of the 'yield from' itself (but it isn't known).
        subexpr_type = get_proper_type(self.accept(e.expr))

        # Check that the expr is an instance of Iterable and get the type of the iterator produced
        # by __iter__.
        if isinstance(subexpr_type, AnyType):
            iter_type: Type = AnyType(TypeOfAny.from_another_any, source_any=subexpr_type)
        elif self.chk.type_is_iterable(subexpr_type):
            if is_async_def(subexpr_type) and not has_coroutine_decorator(return_type):
                self.chk.msg.yield_from_invalid_operand_type(subexpr_type, e)

            any_type = AnyType(TypeOfAny.special_form)
            generic_generator_type = self.chk.named_generic_type(
                "typing.Generator", [any_type, any_type, any_type]
            )
            generic_generator_type.set_line(e)
            iter_type, _ = self.check_method_call_by_name(
                "__iter__", subexpr_type, [], [], context=generic_generator_type
            )
        else:
            if not (is_async_def(subexpr_type) and has_coroutine_decorator(return_type)):
                self.chk.msg.yield_from_invalid_operand_type(subexpr_type, e)
                iter_type = AnyType(TypeOfAny.from_error)
            else:
                iter_type = self.check_awaitable_expr(
                    subexpr_type, e, message_registry.INCOMPATIBLE_TYPES_IN_YIELD_FROM
                )

        # Check that the iterator's item type matches the type yielded by the Generator function
        # containing this `yield from` expression.
        expected_item_type = self.chk.get_generator_yield_type(return_type, False)
        actual_item_type = self.chk.get_generator_yield_type(iter_type, False)

        self.chk.check_subtype(
            actual_item_type,
            expected_item_type,
            e,
            message_registry.INCOMPATIBLE_TYPES_IN_YIELD_FROM,
            "actual type",
            "expected type",
        )

        # Determine the type of the entire yield from expression.
        iter_type = get_proper_type(iter_type)
        expr_type = self.chk.get_generator_return_type(iter_type, is_coroutine=False)

        if not allow_none_return and isinstance(get_proper_type(expr_type), NoneType):
            self.chk.msg.does_not_return_value(None, e)
        return expr_type

    def visit_temp_node(self, e: TempNode) -> Type:
        return e.type

    def visit_type_var_expr(self, e: TypeVarExpr) -> Type:
        p_default = get_proper_type(e.default)
        if not (
            isinstance(p_default, AnyType)
            and p_default.type_of_any == TypeOfAny.from_omitted_generics
        ):
            if not is_subtype(p_default, e.upper_bound):
                self.chk.fail("TypeVar default must be a subtype of the bound type", e)
            if e.values and not any(is_same_type(p_default, value) for value in e.values):
                self.chk.fail("TypeVar default must be one of the constraint types", e)
        return AnyType(TypeOfAny.special_form)

    def visit_paramspec_expr(self, e: ParamSpecExpr) -> Type:
        return AnyType(TypeOfAny.special_form)

    def visit_type_var_tuple_expr(self, e: TypeVarTupleExpr) -> Type:
        return AnyType(TypeOfAny.special_form)

    def visit_newtype_expr(self, e: NewTypeExpr) -> Type:
        return AnyType(TypeOfAny.special_form)

    def visit_namedtuple_expr(self, e: NamedTupleExpr) -> Type:
        tuple_type = e.info.tuple_type
        if tuple_type:
            if self.chk.options.disallow_any_unimported and has_any_from_unimported_type(
                tuple_type
            ):
                self.msg.unimported_type_becomes_any("NamedTuple type", tuple_type, e)
            check_for_explicit_any(
                tuple_type, self.chk.options, self.chk.is_typeshed_stub, self.msg, context=e
            )
        return AnyType(TypeOfAny.special_form)

    def visit_enum_call_expr(self, e: EnumCallExpr) -> Type:
        for name, value in zip(e.items, e.values):
            if value is not None:
                typ = self.accept(value)
                if not isinstance(get_proper_type(typ), AnyType):
                    var = e.info.names[name].node
                    if isinstance(var, Var):
                        # Inline TypeChecker.set_inferred_type(),
                        # without the lvalue.  (This doesn't really do
                        # much, since the value attribute is defined
                        # to have type Any in the typeshed stub.)
                        var.type = typ
                        var.is_inferred = True
        return AnyType(TypeOfAny.special_form)

    def visit_typeddict_expr(self, e: TypedDictExpr) -> Type:
        return AnyType(TypeOfAny.special_form)

    def visit__promote_expr(self, e: PromoteExpr) -> Type:
        return e.type

    def visit_star_expr(self, e: StarExpr) -> Type:
        # TODO: should this ever be called (see e.g. mypyc visitor)?
        return self.accept(e.expr)

    def object_type(self) -> Instance:
        """Return instance type 'object'."""
        return self.named_type("builtins.object")

    def bool_type(self) -> Instance:
        """Return instance type 'bool'."""
        return self.named_type("builtins.bool")

    @overload
    def narrow_type_from_binder(self, expr: Expression, known_type: Type) -> Type: ...

    @overload
    def narrow_type_from_binder(
        self, expr: Expression, known_type: Type, skip_non_overlapping: bool
    ) -> Type | None: ...

    def narrow_type_from_binder(
        self, expr: Expression, known_type: Type, skip_non_overlapping: bool = False
    ) -> Type | None:
        """Narrow down a known type of expression using information in conditional type binder.

        If 'skip_non_overlapping' is True, return None if the type and restriction are
        non-overlapping.
        """
        if literal(expr) >= LITERAL_TYPE:
            restriction = self.chk.binder.get(expr)
            # If the current node is deferred, some variables may get Any types that they
            # otherwise wouldn't have. We don't want to narrow down these since it may
            # produce invalid inferred Optional[Any] types, at least.
            if restriction and not (
                isinstance(get_proper_type(known_type), AnyType) and self.chk.current_node_deferred
            ):
                # Note: this call should match the one in narrow_declared_type().
                if skip_non_overlapping and not is_overlapping_types(
                    known_type, restriction, prohibit_none_typevar_overlap=True
                ):
                    return None
                narrowed = narrow_declared_type(known_type, restriction)
                if isinstance(get_proper_type(narrowed), UninhabitedType):
                    # If we hit this case, it means that we can't reliably mark the code as
                    # unreachable, but the resulting type can't be expressed in type system.
                    # Falling back to restriction is more intuitive in most cases.
                    return restriction
                return narrowed
        return known_type

    def has_abstract_type_part(self, caller_type: ProperType, callee_type: ProperType) -> bool:
        # TODO: support other possible types here
        if isinstance(caller_type, TupleType) and isinstance(callee_type, TupleType):
            return any(
                self.has_abstract_type(get_proper_type(caller), get_proper_type(callee))
                for caller, callee in zip(caller_type.items, callee_type.items)
            )
        return self.has_abstract_type(caller_type, callee_type)

    def has_abstract_type(self, caller_type: ProperType, callee_type: ProperType) -> bool:
        return (
            isinstance(caller_type, FunctionLike)
            and isinstance(callee_type, TypeType)
            and caller_type.is_type_obj()
            and (caller_type.type_object().is_abstract or caller_type.type_object().is_protocol)
            and isinstance(callee_type.item, Instance)
            and (callee_type.item.type.is_abstract or callee_type.item.type.is_protocol)
            and not self.chk.allow_abstract_call
        )


def has_any_type(t: Type, ignore_in_type_obj: bool = False) -> bool:
    """Whether t contains an Any type"""
    return t.accept(HasAnyType(ignore_in_type_obj))


class HasAnyType(types.BoolTypeQuery):
    def __init__(self, ignore_in_type_obj: bool) -> None:
        super().__init__(types.ANY_STRATEGY)
        self.ignore_in_type_obj = ignore_in_type_obj

    def visit_any(self, t: AnyType) -> bool:
        return t.type_of_any != TypeOfAny.special_form  # special forms are not real Any types

    def visit_callable_type(self, t: CallableType) -> bool:
        if self.ignore_in_type_obj and t.is_type_obj():
            return False
        return super().visit_callable_type(t)

    def visit_type_var(self, t: TypeVarType) -> bool:
        default = [t.default] if t.has_default() else []
        return self.query_types([t.upper_bound, *default] + t.values)

    def visit_param_spec(self, t: ParamSpecType) -> bool:
        default = [t.default] if t.has_default() else []
        return self.query_types([t.upper_bound, *default])

    def visit_type_var_tuple(self, t: TypeVarTupleType) -> bool:
        default = [t.default] if t.has_default() else []
        return self.query_types([t.upper_bound, *default])


def has_coroutine_decorator(t: Type) -> bool:
    """Whether t came from a function decorated with `@coroutine`."""
    t = get_proper_type(t)
    return isinstance(t, Instance) and t.type.fullname == "typing.AwaitableGenerator"


def is_async_def(t: Type) -> bool:
    """Whether t came from a function defined using `async def`."""
    # In check_func_def(), when we see a function decorated with
    # `@typing.coroutine` or `@async.coroutine`, we change the
    # return type to typing.AwaitableGenerator[...], so that its
    # type is compatible with either Generator or Awaitable.
    # But for the check here we need to know whether the original
    # function (before decoration) was an `async def`.  The
    # AwaitableGenerator type conveniently preserves the original
    # type as its 4th parameter (3rd when using 0-origin indexing
    # :-), so that we can recover that information here.
    # (We really need to see whether the original, undecorated
    # function was an `async def`, which is orthogonal to its
    # decorations.)
    t = get_proper_type(t)
    if (
        isinstance(t, Instance)
        and t.type.fullname == "typing.AwaitableGenerator"
        and len(t.args) >= 4
    ):
        t = get_proper_type(t.args[3])
    return isinstance(t, Instance) and t.type.fullname == "typing.Coroutine"


def is_non_empty_tuple(t: Type) -> bool:
    t = get_proper_type(t)
    return isinstance(t, TupleType) and bool(t.items)


def is_duplicate_mapping(
    mapping: list[int], actual_types: list[Type], actual_kinds: list[ArgKind]
) -> bool:
    return (
        len(mapping) > 1
        # Multiple actuals can map to the same formal if they both come from
        # varargs (*args and **kwargs); in this case at runtime it is possible
        # that here are no duplicates. We need to allow this, as the convention
        # f(..., *args, **kwargs) is common enough.
        and not (
            len(mapping) == 2
            and actual_kinds[mapping[0]] == nodes.ARG_STAR
            and actual_kinds[mapping[1]] == nodes.ARG_STAR2
        )
        # Multiple actuals can map to the same formal if there are multiple
        # **kwargs which cannot be mapped with certainty (non-TypedDict
        # **kwargs).
        and not all(
            actual_kinds[m] == nodes.ARG_STAR2
            and not isinstance(get_proper_type(actual_types[m]), TypedDictType)
            for m in mapping
        )
    )


def replace_callable_return_type(c: CallableType, new_ret_type: Type) -> CallableType:
    """Return a copy of a callable type with a different return type."""
    return c.copy_modified(ret_type=new_ret_type)


class ArgInferSecondPassQuery(types.BoolTypeQuery):
    """Query whether an argument type should be inferred in the second pass.

    The result is True if the type has a type variable in a callable return
    type anywhere. For example, the result for Callable[[], T] is True if t is
    a type variable.
    """

    def __init__(self) -> None:
        super().__init__(types.ANY_STRATEGY)

    def visit_callable_type(self, t: CallableType) -> bool:
        # TODO: we need to check only for type variables of original callable.
        return self.query_types(t.arg_types) or has_type_vars(t)


def has_erased_component(t: Type | None) -> bool:
    return t is not None and t.accept(HasErasedComponentsQuery())


class HasErasedComponentsQuery(types.BoolTypeQuery):
    """Visitor for querying whether a type has an erased component."""

    def __init__(self) -> None:
        super().__init__(types.ANY_STRATEGY)

    def visit_erased_type(self, t: ErasedType) -> bool:
        return True


def has_uninhabited_component(t: Type | None) -> bool:
    return t is not None and t.accept(HasUninhabitedComponentsQuery())


class HasUninhabitedComponentsQuery(types.BoolTypeQuery):
    """Visitor for querying whether a type has an UninhabitedType component."""

    def __init__(self) -> None:
        super().__init__(types.ANY_STRATEGY)

    def visit_uninhabited_type(self, t: UninhabitedType) -> bool:
        return True


def arg_approximate_similarity(actual: Type, formal: Type) -> bool:
    """Return if caller argument (actual) is roughly compatible with signature arg (formal).

    This function is deliberately loose and will report two types are similar
    as long as their "shapes" are plausibly the same.

    This is useful when we're doing error reporting: for example, if we're trying
    to select an overload alternative and there's no exact match, we can use
    this function to help us identify which alternative the user might have
    *meant* to match.
    """
    actual = get_proper_type(actual)
    formal = get_proper_type(formal)

    # Erase typevars: we'll consider them all to have the same "shape".
    if isinstance(actual, TypeVarType):
        actual = erase_to_union_or_bound(actual)
    if isinstance(formal, TypeVarType):
        formal = erase_to_union_or_bound(formal)

    # Callable or Type[...]-ish types
    def is_typetype_like(typ: ProperType) -> bool:
        return (
            isinstance(typ, TypeType)
            or (isinstance(typ, FunctionLike) and typ.is_type_obj())
            or (isinstance(typ, Instance) and typ.type.fullname == "builtins.type")
        )

    if isinstance(formal, CallableType):
        if isinstance(actual, (CallableType, Overloaded, TypeType)):
            return True
    if is_typetype_like(actual) and is_typetype_like(formal):
        return True

    # Unions
    if isinstance(actual, UnionType):
        return any(arg_approximate_similarity(item, formal) for item in actual.relevant_items())
    if isinstance(formal, UnionType):
        return any(arg_approximate_similarity(actual, item) for item in formal.relevant_items())

    # TypedDicts
    if isinstance(actual, TypedDictType):
        if isinstance(formal, TypedDictType):
            return True
        return arg_approximate_similarity(actual.fallback, formal)

    # Instances
    # For instances, we mostly defer to the existing is_subtype check.
    if isinstance(formal, Instance):
        if isinstance(actual, CallableType):
            actual = actual.fallback
        if isinstance(actual, Overloaded):
            actual = actual.items[0].fallback
        if isinstance(actual, TupleType):
            actual = tuple_fallback(actual)
        if isinstance(actual, Instance) and formal.type in actual.type.mro:
            # Try performing a quick check as an optimization
            return True

    # Fall back to a standard subtype check for the remaining kinds of type.
    return is_subtype(erasetype.erase_type(actual), erasetype.erase_type(formal))


def any_causes_overload_ambiguity(
    items: list[CallableType],
    return_types: list[Type],
    arg_types: list[Type],
    arg_kinds: list[ArgKind],
    arg_names: Sequence[str | None] | None,
) -> bool:
    """May an argument containing 'Any' cause ambiguous result type on call to overloaded function?

    Note that this sometimes returns True even if there is no ambiguity, since a correct
    implementation would be complex (and the call would be imprecisely typed due to Any
    types anyway).

    Args:
        items: Overload items matching the actual arguments
        arg_types: Actual argument types
        arg_kinds: Actual argument kinds
        arg_names: Actual argument names
    """
    if all_same_types(return_types):
        return False

    actual_to_formal = [
        map_formals_to_actuals(
            arg_kinds, arg_names, item.arg_kinds, item.arg_names, lambda i: arg_types[i]
        )
        for item in items
    ]

    for arg_idx, arg_type in enumerate(arg_types):
        # We ignore Anys in type object callables as ambiguity
        # creators, since that can lead to falsely claiming ambiguity
        # for overloads between Type and Callable.
        if has_any_type(arg_type, ignore_in_type_obj=True):
            matching_formals_unfiltered = [
                (item_idx, lookup[arg_idx])
                for item_idx, lookup in enumerate(actual_to_formal)
                if lookup[arg_idx]
            ]

            matching_returns = []
            matching_formals = []
            for item_idx, formals in matching_formals_unfiltered:
                matched_callable = items[item_idx]
                matching_returns.append(matched_callable.ret_type)

                # Note: if an actual maps to multiple formals of differing types within
                # a single callable, then we know at least one of those formals must be
                # a different type then the formal(s) in some other callable.
                # So it's safe to just append everything to the same list.
                for formal in formals:
                    matching_formals.append(matched_callable.arg_types[formal])
            if not all_same_types(matching_formals) and not all_same_types(matching_returns):
                # Any maps to multiple different types, and the return types of these items differ.
                return True
    return False


def all_same_types(types: list[Type]) -> bool:
    if not types:
        return True
    return all(is_same_type(t, types[0]) for t in types[1:])


def merge_typevars_in_callables_by_name(
    callables: Sequence[CallableType],
) -> tuple[list[CallableType], list[TypeVarType]]:
    """Takes all the typevars present in the callables and 'combines' the ones with the same name.

    For example, suppose we have two callables with signatures "f(x: T, y: S) -> T" and
    "f(x: List[Tuple[T, S]]) -> Tuple[T, S]". Both callables use typevars named "T" and
    "S", but we treat them as distinct, unrelated typevars. (E.g. they could both have
    distinct ids.)

    If we pass in both callables into this function, it returns a list containing two
    new callables that are identical in signature, but use the same underlying TypeVarType
    for T and S.

    This is useful if we want to take the output lists and "merge" them into one callable
    in some way -- for example, when unioning together overloads.

    Returns both the new list of callables and a list of all distinct TypeVarType objects used.
    """
    output: list[CallableType] = []
    unique_typevars: dict[str, TypeVarType] = {}
    variables: list[TypeVarType] = []

    for target in callables:
        if target.is_generic():
            target = freshen_function_type_vars(target)

            rename = {}  # Dict[TypeVarId, TypeVar]
            for tv in target.variables:
                name = tv.fullname
                if name not in unique_typevars:
                    # TODO: support ParamSpecType and TypeVarTuple.
                    if isinstance(tv, (ParamSpecType, TypeVarTupleType)):
                        continue
                    assert isinstance(tv, TypeVarType)
                    unique_typevars[name] = tv
                    variables.append(tv)
                rename[tv.id] = unique_typevars[name]

            target = expand_type(target, rename)
        output.append(target)

    return output, variables


def try_getting_literal(typ: Type) -> ProperType:
    """If possible, get a more precise literal type for a given type."""
    typ = get_proper_type(typ)
    if isinstance(typ, Instance) and typ.last_known_value is not None:
        return typ.last_known_value
    return typ


def is_expr_literal_type(node: Expression) -> bool:
    """Returns 'true' if the given node is a Literal"""
    if isinstance(node, IndexExpr):
        base = node.base
        return isinstance(base, RefExpr) and base.fullname in LITERAL_TYPE_NAMES
    if isinstance(node, NameExpr):
        underlying = node.node
        return isinstance(underlying, TypeAlias) and isinstance(
            get_proper_type(underlying.target), LiteralType
        )
    return False


def has_bytes_component(typ: Type) -> bool:
    """Is this one of builtin byte types, or a union that contains it?"""
    typ = get_proper_type(typ)
    byte_types = {"builtins.bytes", "builtins.bytearray"}
    if isinstance(typ, UnionType):
        return any(has_bytes_component(t) for t in typ.items)
    if isinstance(typ, Instance) and typ.type.fullname in byte_types:
        return True
    return False


def type_info_from_type(typ: Type) -> TypeInfo | None:
    """Gets the TypeInfo for a type, indirecting through things like type variables and tuples."""
    typ = get_proper_type(typ)
    if isinstance(typ, FunctionLike) and typ.is_type_obj():
        return typ.type_object()
    if isinstance(typ, TypeType):
        typ = typ.item
    if isinstance(typ, TypeVarType):
        typ = get_proper_type(typ.upper_bound)
    if isinstance(typ, TupleType):
        typ = tuple_fallback(typ)
    if isinstance(typ, Instance):
        return typ.type

    # A complicated type. Too tricky, give up.
    # TODO: Do something more clever here.
    return None


def is_operator_method(fullname: str | None) -> bool:
    if not fullname:
        return False
    short_name = fullname.split(".")[-1]
    return (
        short_name in operators.op_methods.values()
        or short_name in operators.reverse_op_methods.values()
        or short_name in operators.unary_op_methods.values()
    )


def get_partial_instance_type(t: Type | None) -> PartialType | None:
    if t is None or not isinstance(t, PartialType) or t.type is None:
        return None
    return t


def is_type_type_context(context: Type | None) -> bool:
    context = get_proper_type(context)
    if isinstance(context, TypeType):
        return True
    if isinstance(context, UnionType):
        return any(is_type_type_context(item) for item in context.items)
    return False
