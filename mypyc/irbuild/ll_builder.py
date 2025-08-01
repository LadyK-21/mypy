"""A "low-level" IR builder class.

See the docstring of class LowLevelIRBuilder for more information.

"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Callable, Final, Optional

from mypy.argmap import map_actuals_to_formals
from mypy.nodes import ARG_POS, ARG_STAR, ARG_STAR2, ArgKind
from mypy.operators import op_methods, unary_op_methods
from mypy.types import AnyType, TypeOfAny
from mypyc.common import (
    BITMAP_BITS,
    FAST_ISINSTANCE_MAX_SUBCLASSES,
    IS_FREE_THREADED,
    MAX_LITERAL_SHORT_INT,
    MAX_SHORT_INT,
    MIN_LITERAL_SHORT_INT,
    MIN_SHORT_INT,
    PLATFORM_SIZE,
)
from mypyc.errors import Errors
from mypyc.ir.class_ir import ClassIR, all_concrete_classes
from mypyc.ir.func_ir import FuncDecl, FuncSignature
from mypyc.ir.ops import (
    ERR_FALSE,
    ERR_NEVER,
    NAMESPACE_MODULE,
    NAMESPACE_STATIC,
    NAMESPACE_TYPE,
    Assign,
    AssignMulti,
    BasicBlock,
    Box,
    Branch,
    Call,
    CallC,
    Cast,
    ComparisonOp,
    Extend,
    Float,
    FloatComparisonOp,
    FloatNeg,
    FloatOp,
    GetAttr,
    GetElementPtr,
    Goto,
    Integer,
    IntOp,
    KeepAlive,
    LoadAddress,
    LoadErrorValue,
    LoadLiteral,
    LoadMem,
    LoadStatic,
    MethodCall,
    Op,
    PrimitiveDescription,
    PrimitiveOp,
    RaiseStandardError,
    Register,
    Truncate,
    TupleGet,
    TupleSet,
    Unbox,
    Unreachable,
    Value,
    float_comparison_op_to_id,
    float_op_to_id,
    int_op_to_id,
)
from mypyc.ir.rtypes import (
    PyObject,
    PySetObject,
    RArray,
    RInstance,
    RPrimitive,
    RTuple,
    RType,
    RUnion,
    bit_rprimitive,
    bitmap_rprimitive,
    bool_rprimitive,
    bytes_rprimitive,
    c_int_rprimitive,
    c_pointer_rprimitive,
    c_pyssize_t_rprimitive,
    c_size_t_rprimitive,
    check_native_int_range,
    dict_rprimitive,
    float_rprimitive,
    int_rprimitive,
    is_bool_or_bit_rprimitive,
    is_bytes_rprimitive,
    is_dict_rprimitive,
    is_fixed_width_rtype,
    is_float_rprimitive,
    is_frozenset_rprimitive,
    is_int16_rprimitive,
    is_int32_rprimitive,
    is_int64_rprimitive,
    is_int_rprimitive,
    is_list_rprimitive,
    is_none_rprimitive,
    is_set_rprimitive,
    is_short_int_rprimitive,
    is_str_rprimitive,
    is_tagged,
    is_tuple_rprimitive,
    is_uint8_rprimitive,
    list_rprimitive,
    none_rprimitive,
    object_pointer_rprimitive,
    object_rprimitive,
    optional_value_type,
    pointer_rprimitive,
    short_int_rprimitive,
    str_rprimitive,
)
from mypyc.irbuild.util import concrete_arg_kind
from mypyc.options import CompilerOptions
from mypyc.primitives.bytes_ops import bytes_compare
from mypyc.primitives.dict_ops import (
    dict_build_op,
    dict_new_op,
    dict_ssize_t_size_op,
    dict_update_in_display_op,
)
from mypyc.primitives.exc_ops import err_occurred_op, keep_propagating_op
from mypyc.primitives.float_ops import copysign_op, int_to_float_op
from mypyc.primitives.generic_ops import (
    generic_len_op,
    generic_ssize_t_len_op,
    py_call_op,
    py_call_with_kwargs_op,
    py_getattr_op,
    py_method_call_op,
    py_vectorcall_method_op,
    py_vectorcall_op,
)
from mypyc.primitives.int_ops import (
    int16_divide_op,
    int16_mod_op,
    int16_overflow,
    int32_divide_op,
    int32_mod_op,
    int32_overflow,
    int64_divide_op,
    int64_mod_op,
    int64_to_int_op,
    int_to_int32_op,
    int_to_int64_op,
    ssize_t_to_int_op,
    uint8_overflow,
)
from mypyc.primitives.list_ops import list_build_op, list_extend_op, list_items, new_list_op
from mypyc.primitives.misc_ops import (
    bool_op,
    buf_init_item,
    debug_print_op,
    fast_isinstance_op,
    none_object_op,
    not_implemented_op,
    set_immortal_op,
    var_object_size,
)
from mypyc.primitives.registry import (
    ERR_NEG_INT,
    CFunctionDescription,
    binary_ops,
    method_call_ops,
    unary_ops,
)
from mypyc.primitives.set_ops import new_set_op
from mypyc.primitives.str_ops import (
    str_check_if_true,
    str_eq,
    str_ssize_t_size_op,
    unicode_compare,
)
from mypyc.primitives.tuple_ops import list_tuple_op, new_tuple_op, new_tuple_with_length_op
from mypyc.rt_subtype import is_runtime_subtype
from mypyc.sametype import is_same_type
from mypyc.subtype import is_subtype

DictEntry = tuple[Optional[Value], Value]

# If the number of items is less than the threshold when initializing
# a list, we would inline the generate IR using SetMem and expanded
# for-loop. Otherwise, we would call `list_build_op` for larger lists.
# TODO: The threshold is a randomly chosen number which needs further
#       study on real-world projects for a better balance.
LIST_BUILDING_EXPANSION_THRESHOLD = 10

# From CPython
PY_VECTORCALL_ARGUMENTS_OFFSET: Final = 1 << (PLATFORM_SIZE * 8 - 1)

FIXED_WIDTH_INT_BINARY_OPS: Final = {
    "+",
    "-",
    "*",
    "//",
    "%",
    "&",
    "|",
    "^",
    "<<",
    ">>",
    "+=",
    "-=",
    "*=",
    "//=",
    "%=",
    "&=",
    "|=",
    "^=",
    "<<=",
    ">>=",
}

# Binary operations on bools that are specialized and don't just promote operands to int
BOOL_BINARY_OPS: Final = {"&", "&=", "|", "|=", "^", "^=", "==", "!=", "<", "<=", ">", ">="}


class LowLevelIRBuilder:
    """A "low-level" IR builder class.

    LowLevelIRBuilder provides core abstractions we use for constructing
    IR as well as a number of higher-level ones (accessing attributes,
    calling functions and methods, and coercing between types, for
    example).

    The core principle of the low-level IR builder is that all of its
    facilities operate solely on the mypyc IR level and not the mypy AST
    level---it has *no knowledge* of mypy types or expressions.

    The mypyc.irbuilder.builder.IRBuilder class wraps an instance of this
    class and provides additional functionality to transform mypy AST nodes
    to IR.
    """

    def __init__(self, errors: Errors | None, options: CompilerOptions) -> None:
        self.errors = errors
        self.options = options
        self.args: list[Register] = []
        self.blocks: list[BasicBlock] = []
        # Stack of except handler entry blocks
        self.error_handlers: list[BasicBlock | None] = [None]
        # Values that we need to keep alive as long as we have borrowed
        # temporaries. Use flush_keep_alives() to mark the end of the live range.
        self.keep_alives: list[Value] = []

    def set_module(self, module_name: str, module_path: str) -> None:
        """Set the name and path of the current module."""
        self.module_name = module_name
        self.module_path = module_path

    # Basic operations

    def add(self, op: Op) -> Value:
        """Add an op."""
        assert not self.blocks[-1].terminated, "Can't add to finished block"
        self.blocks[-1].ops.append(op)
        return op

    def goto(self, target: BasicBlock) -> None:
        """Add goto to a basic block."""
        if not self.blocks[-1].terminated:
            self.add(Goto(target))

    def activate_block(self, block: BasicBlock) -> None:
        """Add a basic block and make it the active one (target of adds)."""
        if self.blocks:
            assert self.blocks[-1].terminated

        block.error_handler = self.error_handlers[-1]
        self.blocks.append(block)

    def goto_and_activate(self, block: BasicBlock) -> None:
        """Add goto a block and make it the active block."""
        self.goto(block)
        self.activate_block(block)

    def keep_alive(self, values: list[Value], *, steal: bool = False) -> None:
        self.add(KeepAlive(values, steal=steal))

    def load_mem(self, ptr: Value, value_type: RType, *, borrow: bool = False) -> Value:
        return self.add(LoadMem(value_type, ptr, borrow=borrow))

    def push_error_handler(self, handler: BasicBlock | None) -> None:
        self.error_handlers.append(handler)

    def pop_error_handler(self) -> BasicBlock | None:
        return self.error_handlers.pop()

    def self(self) -> Register:
        """Return reference to the 'self' argument.

        This only works in a method.
        """
        return self.args[0]

    def flush_keep_alives(self) -> None:
        if self.keep_alives:
            self.add(KeepAlive(self.keep_alives.copy()))
            self.keep_alives = []

    def debug_print(self, toprint: str | Value) -> None:
        if isinstance(toprint, str):
            toprint = self.load_str(toprint)
        self.primitive_op(debug_print_op, [toprint], -1)

    # Type conversions

    def box(self, src: Value) -> Value:
        if src.type.is_unboxed:
            if isinstance(src, Integer) and is_tagged(src.type):
                return self.add(LoadLiteral(src.value >> 1, rtype=object_rprimitive))
            return self.add(Box(src))
        else:
            return src

    def unbox_or_cast(
        self, src: Value, target_type: RType, line: int, *, can_borrow: bool = False
    ) -> Value:
        if target_type.is_unboxed:
            return self.add(Unbox(src, target_type, line))
        else:
            if can_borrow:
                self.keep_alives.append(src)
            return self.add(Cast(src, target_type, line, borrow=can_borrow))

    def coerce(
        self,
        src: Value,
        target_type: RType,
        line: int,
        force: bool = False,
        *,
        can_borrow: bool = False,
    ) -> Value:
        """Generate a coercion/cast from one type to other (only if needed).

        For example, int -> object boxes the source int; int -> int emits nothing;
        object -> int unboxes the object. All conversions preserve object value.

        If force is true, always generate an op (even if it is just an assignment) so
        that the result will have exactly target_type as the type.

        Returns the register with the converted value (may be same as src).
        """
        src_type = src.type
        if src_type.is_unboxed and not target_type.is_unboxed:
            # Unboxed -> boxed
            return self.box(src)
        if (src_type.is_unboxed and target_type.is_unboxed) and not is_runtime_subtype(
            src_type, target_type
        ):
            if (
                isinstance(src, Integer)
                and is_short_int_rprimitive(src_type)
                and is_fixed_width_rtype(target_type)
            ):
                value = src.numeric_value()
                if not check_native_int_range(target_type, value):
                    self.error(f'Value {value} is out of range for "{target_type}"', line)
                return Integer(src.value >> 1, target_type)
            elif is_int_rprimitive(src_type) and is_fixed_width_rtype(target_type):
                return self.coerce_int_to_fixed_width(src, target_type, line)
            elif is_fixed_width_rtype(src_type) and is_int_rprimitive(target_type):
                return self.coerce_fixed_width_to_int(src, line)
            elif is_short_int_rprimitive(src_type) and is_fixed_width_rtype(target_type):
                return self.coerce_short_int_to_fixed_width(src, target_type, line)
            elif (
                isinstance(src_type, RPrimitive)
                and isinstance(target_type, RPrimitive)
                and src_type.is_native_int
                and target_type.is_native_int
                and src_type.size == target_type.size
                and src_type.is_signed == target_type.is_signed
            ):
                # Equivalent types
                return src
            elif is_bool_or_bit_rprimitive(src_type) and is_tagged(target_type):
                shifted = self.int_op(
                    bool_rprimitive, src, Integer(1, bool_rprimitive), IntOp.LEFT_SHIFT
                )
                return self.add(Extend(shifted, target_type, signed=False))
            elif is_bool_or_bit_rprimitive(src_type) and is_fixed_width_rtype(target_type):
                return self.add(Extend(src, target_type, signed=False))
            elif isinstance(src, Integer) and is_float_rprimitive(target_type):
                if is_tagged(src_type):
                    return Float(float(src.value // 2))
                return Float(float(src.value))
            elif is_tagged(src_type) and is_float_rprimitive(target_type):
                return self.int_to_float(src, line)
            elif (
                isinstance(src_type, RTuple)
                and isinstance(target_type, RTuple)
                and len(src_type.types) == len(target_type.types)
            ):
                # Coerce between two tuple types by coercing each item separately
                values = []
                for i in range(len(src_type.types)):
                    v = None
                    if isinstance(src, TupleSet):
                        item = src.items[i]
                        # We can't reuse register values, since they can be modified.
                        if not isinstance(item, Register):
                            v = item
                    if v is None:
                        v = TupleGet(src, i)
                        self.add(v)
                    values.append(v)
                return self.add(
                    TupleSet(
                        [self.coerce(v, t, line) for v, t in zip(values, target_type.types)], line
                    )
                )
            # To go between any other unboxed types, we go through a boxed
            # in-between value, for simplicity.
            tmp = self.box(src)
            return self.unbox_or_cast(tmp, target_type, line)
        if (not src_type.is_unboxed and target_type.is_unboxed) or not is_subtype(
            src_type, target_type
        ):
            return self.unbox_or_cast(src, target_type, line, can_borrow=can_borrow)
        elif force:
            tmp = Register(target_type)
            self.add(Assign(tmp, src))
            return tmp
        return src

    def coerce_int_to_fixed_width(self, src: Value, target_type: RType, line: int) -> Value:
        assert is_fixed_width_rtype(target_type), target_type
        assert isinstance(target_type, RPrimitive), target_type

        res = Register(target_type)

        fast, slow, end = BasicBlock(), BasicBlock(), BasicBlock()

        check = self.check_tagged_short_int(src, line)
        self.add(Branch(check, fast, slow, Branch.BOOL))

        self.activate_block(fast)

        size = target_type.size
        if size < int_rprimitive.size:
            # Add a range check when the target type is smaller than the source type
            fast2, fast3 = BasicBlock(), BasicBlock()
            upper_bound = 1 << (size * 8 - 1)
            if not target_type.is_signed:
                upper_bound *= 2
            check2 = self.add(ComparisonOp(src, Integer(upper_bound, src.type), ComparisonOp.SLT))
            self.add(Branch(check2, fast2, slow, Branch.BOOL))
            self.activate_block(fast2)
            if target_type.is_signed:
                lower_bound = -upper_bound
            else:
                lower_bound = 0
            check3 = self.add(ComparisonOp(src, Integer(lower_bound, src.type), ComparisonOp.SGE))
            self.add(Branch(check3, fast3, slow, Branch.BOOL))
            self.activate_block(fast3)
            tmp = self.int_op(
                c_pyssize_t_rprimitive,
                src,
                Integer(1, c_pyssize_t_rprimitive),
                IntOp.RIGHT_SHIFT,
                line,
            )
            tmp = self.add(Truncate(tmp, target_type))
        else:
            if size > int_rprimitive.size:
                tmp = self.add(Extend(src, target_type, signed=True))
            else:
                tmp = src
            tmp = self.int_op(target_type, tmp, Integer(1, target_type), IntOp.RIGHT_SHIFT, line)

        self.add(Assign(res, tmp))
        self.goto(end)

        self.activate_block(slow)
        if is_int64_rprimitive(target_type) or (
            is_int32_rprimitive(target_type) and size == int_rprimitive.size
        ):
            # Slow path calls a library function that handles more complex logic
            ptr = self.int_op(
                pointer_rprimitive, src, Integer(1, pointer_rprimitive), IntOp.XOR, line
            )
            ptr2 = Register(c_pointer_rprimitive)
            self.add(Assign(ptr2, ptr))
            if is_int64_rprimitive(target_type):
                conv_op = int_to_int64_op
            else:
                conv_op = int_to_int32_op
            tmp = self.call_c(conv_op, [ptr2], line)
            self.add(Assign(res, tmp))
            self.add(KeepAlive([src]))
            self.goto(end)
        elif is_int32_rprimitive(target_type):
            # Slow path just always generates an OverflowError
            self.call_c(int32_overflow, [], line)
            self.add(Unreachable())
        elif is_int16_rprimitive(target_type):
            # Slow path just always generates an OverflowError
            self.call_c(int16_overflow, [], line)
            self.add(Unreachable())
        elif is_uint8_rprimitive(target_type):
            # Slow path just always generates an OverflowError
            self.call_c(uint8_overflow, [], line)
            self.add(Unreachable())
        else:
            assert False, target_type

        self.activate_block(end)
        return res

    def coerce_short_int_to_fixed_width(self, src: Value, target_type: RType, line: int) -> Value:
        if is_int64_rprimitive(target_type) or (
            PLATFORM_SIZE == 4 and is_int32_rprimitive(target_type)
        ):
            return self.int_op(target_type, src, Integer(1, target_type), IntOp.RIGHT_SHIFT, line)
        # TODO: i32 on 64-bit platform
        assert False, (src.type, target_type, PLATFORM_SIZE)

    def coerce_fixed_width_to_int(self, src: Value, line: int) -> Value:
        if (
            (is_int32_rprimitive(src.type) and PLATFORM_SIZE == 8)
            or is_int16_rprimitive(src.type)
            or is_uint8_rprimitive(src.type)
        ):
            # Simple case -- just sign extend and shift.
            extended = self.add(Extend(src, c_pyssize_t_rprimitive, signed=src.type.is_signed))
            return self.int_op(
                int_rprimitive,
                extended,
                Integer(1, c_pyssize_t_rprimitive),
                IntOp.LEFT_SHIFT,
                line,
            )

        src_type = src.type

        assert is_fixed_width_rtype(src_type), src_type
        assert isinstance(src_type, RPrimitive), src_type

        res = Register(int_rprimitive)

        fast, fast2, slow, end = BasicBlock(), BasicBlock(), BasicBlock(), BasicBlock()

        c1 = self.add(ComparisonOp(src, Integer(MAX_SHORT_INT, src_type), ComparisonOp.SLE))
        self.add(Branch(c1, fast, slow, Branch.BOOL))

        self.activate_block(fast)
        c2 = self.add(ComparisonOp(src, Integer(MIN_SHORT_INT, src_type), ComparisonOp.SGE))
        self.add(Branch(c2, fast2, slow, Branch.BOOL))

        self.activate_block(slow)
        if is_int64_rprimitive(src_type):
            conv_op = int64_to_int_op
        elif is_int32_rprimitive(src_type):
            assert PLATFORM_SIZE == 4
            conv_op = ssize_t_to_int_op
        else:
            assert False, src_type
        x = self.call_c(conv_op, [src], line)
        self.add(Assign(res, x))
        self.goto(end)

        self.activate_block(fast2)
        if int_rprimitive.size < src_type.size:
            tmp = self.add(Truncate(src, c_pyssize_t_rprimitive))
        else:
            tmp = src
        s = self.int_op(int_rprimitive, tmp, Integer(1, tmp.type), IntOp.LEFT_SHIFT, line)
        self.add(Assign(res, s))
        self.goto(end)

        self.activate_block(end)
        return res

    def coerce_nullable(self, src: Value, target_type: RType, line: int) -> Value:
        """Generate a coercion from a potentially null value."""
        if src.type.is_unboxed == target_type.is_unboxed and (
            (target_type.is_unboxed and is_runtime_subtype(src.type, target_type))
            or (not target_type.is_unboxed and is_subtype(src.type, target_type))
        ):
            return src

        target = Register(target_type)

        valid, invalid, out = BasicBlock(), BasicBlock(), BasicBlock()
        self.add(Branch(src, invalid, valid, Branch.IS_ERROR))

        self.activate_block(valid)
        coerced = self.coerce(src, target_type, line)
        self.add(Assign(target, coerced, line))
        self.goto(out)

        self.activate_block(invalid)
        error = self.add(LoadErrorValue(target_type))
        self.add(Assign(target, error, line))

        self.goto_and_activate(out)
        return target

    # Attribute access

    def get_attr(
        self, obj: Value, attr: str, result_type: RType, line: int, *, borrow: bool = False
    ) -> Value:
        """Get a native or Python attribute of an object."""
        if (
            isinstance(obj.type, RInstance)
            and obj.type.class_ir.is_ext_class
            and obj.type.class_ir.has_attr(attr)
        ):
            op = GetAttr(obj, attr, line, borrow=borrow)
            # For non-refcounted attribute types, the borrow might be
            # disabled even if requested, so don't check 'borrow'.
            if op.is_borrowed:
                self.keep_alives.append(obj)
            return self.add(op)
        elif isinstance(obj.type, RUnion):
            return self.union_get_attr(obj, obj.type, attr, result_type, line)
        else:
            return self.py_get_attr(obj, attr, line)

    def union_get_attr(
        self, obj: Value, rtype: RUnion, attr: str, result_type: RType, line: int
    ) -> Value:
        """Get an attribute of an object with a union type."""

        def get_item_attr(value: Value) -> Value:
            return self.get_attr(value, attr, result_type, line)

        return self.decompose_union_helper(obj, rtype, result_type, get_item_attr, line)

    def py_get_attr(self, obj: Value, attr: str, line: int) -> Value:
        """Get a Python attribute (slow).

        Prefer get_attr() which generates optimized code for native classes.
        """
        key = self.load_str(attr)
        return self.primitive_op(py_getattr_op, [obj, key], line)

    # isinstance() checks

    def isinstance_helper(self, obj: Value, class_irs: list[ClassIR], line: int) -> Value:
        """Fast path for isinstance() that checks against a list of native classes."""
        if not class_irs:
            return self.false()
        ret = self.isinstance_native(obj, class_irs[0], line)
        for class_ir in class_irs[1:]:

            def other() -> Value:
                return self.isinstance_native(obj, class_ir, line)

            ret = self.shortcircuit_helper("or", bool_rprimitive, lambda: ret, other, line)
        return ret

    def get_type_of_obj(self, obj: Value, line: int) -> Value:
        ob_type_address = self.add(GetElementPtr(obj, PyObject, "ob_type", line))
        ob_type = self.load_mem(ob_type_address, object_rprimitive, borrow=True)
        self.add(KeepAlive([obj]))
        return ob_type

    def type_is_op(self, obj: Value, type_obj: Value, line: int) -> Value:
        typ = self.get_type_of_obj(obj, line)
        return self.add(ComparisonOp(typ, type_obj, ComparisonOp.EQ, line))

    def isinstance_native(self, obj: Value, class_ir: ClassIR, line: int) -> Value:
        """Fast isinstance() check for a native class.

        If there are three or fewer concrete (non-trait) classes among the class
        and all its children, use even faster type comparison checks `type(obj)
        is typ`.
        """
        concrete = all_concrete_classes(class_ir)
        if concrete is None or len(concrete) > FAST_ISINSTANCE_MAX_SUBCLASSES + 1:
            return self.primitive_op(
                fast_isinstance_op, [obj, self.get_native_type(class_ir)], line
            )
        if not concrete:
            # There can't be any concrete instance that matches this.
            return self.false()
        type_obj = self.get_native_type(concrete[0])
        ret = self.type_is_op(obj, type_obj, line)
        for c in concrete[1:]:

            def other() -> Value:
                return self.type_is_op(obj, self.get_native_type(c), line)

            ret = self.shortcircuit_helper("or", bool_rprimitive, lambda: ret, other, line)
        return ret

    # Calls

    def _construct_varargs(
        self,
        args: Sequence[tuple[Value, ArgKind, str | None]],
        line: int,
        *,
        has_star: bool,
        has_star2: bool,
    ) -> tuple[Value | None, Value | None]:
        """Construct *args and **kwargs from a collection of arguments

        This is pretty complicated, and almost all of the complication here stems from
        one of two things (but mostly the second):
          * The handling of ARG_STAR/ARG_STAR2. We want to create as much of the args/kwargs
            values in one go as we can, so we collect values until our hand is forced, and
            then we emit creation of the list/tuple, and expand it from there if needed.

          * Support potentially nullable argument values. This has very narrow applicability,
            as this will never be done by our compiled Python code, but is critically used
            by gen_glue_method when generating glue methods to mediate between the function
            signature of a parent class and its subclasses.

            For named-only arguments, this is quite simple: if it is
            null, don't put it in the dict.

            For positional-or-named arguments, things are much more complicated.
              * First, anything that was passed as a positional arg
                must be forwarded along as a positional arg. It *must
                not* be converted to a named arg. This is because mypy
                does not enforce that positional-or-named arguments
                have the same name in subclasses, and it is not
                uncommon for code to have different names in
                subclasses (a bunch of mypy's visitors do this, for
                example!). This is arguably a bug in both mypy and code doing
                this, and they ought to be using positional-only arguments, but
                positional-only arguments are new and ugly.

              * On the flip side, we're willing to accept the
                infelicity of sometimes turning an argument that was
                passed by keyword into a positional argument. It's wrong,
                but it's very marginal, and avoiding it would require passing
                a bitmask of which arguments were named with every function call,
                or something similar.
                (See some discussion of this in testComplicatedArgs)

            Thus, our strategy for positional-or-named arguments is to
            always pass them as positional, except in the one
            situation where we can not, and where we can be absolutely
            sure they were passed by name: when an *earlier*
            positional argument was missing its value.

            This means that if we have a method `f(self, x: int=..., y: object=...)`:
              * x and y present:      args=(x, y), kwargs={}
              * x present, y missing: args=(x,),   kwargs={}
              * x missing, y present: args=(),     kwargs={'y': y}

            To implement this, when we have multiple optional
            positional arguments, we maintain a flag in a register
            that tracks whether an argument has been missing, and for
            each such optional argument (except the first), we check
            the flag to determine whether to append the argument to
            the *args list or add it to the **kwargs dict. What a
            mess!

            This is what really makes everything here such a tangle;
            otherwise the *args and **kwargs code could be separated.

        The arguments has_star and has_star2 indicate whether the target function
        takes an ARG_STAR and ARG_STAR2 argument, respectively.
        (These will always be true when making a pycall, and be based
        on the actual target signature for a native call.)
        """

        star_result: Value | None = None
        star2_result: Value | None = None
        # We aggregate values that need to go into *args and **kwargs
        # in these lists. Once all arguments are processed (in the
        # happiest case), or we encounter an ARG_STAR/ARG_STAR2 or a
        # nullable arg, then we create the list and/or dict.
        star_values: list[Value] = []
        star2_keys: list[Value] = []
        star2_values: list[Value] = []

        seen_empty_reg: Register | None = None

        for value, kind, name in args:
            if kind == ARG_STAR:
                if star_result is None:
                    star_result = self.new_list_op(star_values, line)
                self.primitive_op(list_extend_op, [star_result, value], line)
            elif kind == ARG_STAR2:
                if star2_result is None:
                    star2_result = self._create_dict(star2_keys, star2_values, line)

                self.call_c(dict_update_in_display_op, [star2_result, value], line=line)
            else:
                nullable = kind.is_optional()
                maybe_pos = kind.is_positional() and has_star
                maybe_named = kind.is_named() or (kind.is_optional() and name and has_star2)

                # If the argument is nullable, we need to create the
                # relevant args/kwargs objects so that we can
                # conditionally modify them.
                if nullable:
                    if maybe_pos and star_result is None:
                        star_result = self.new_list_op(star_values, line)
                    if maybe_named and star2_result is None:
                        star2_result = self._create_dict(star2_keys, star2_values, line)

                # Easy cases: just collect the argument.
                if maybe_pos and star_result is None:
                    star_values.append(value)
                    continue

                if maybe_named and star2_result is None:
                    assert name is not None
                    key = self.load_str(name)
                    star2_keys.append(key)
                    star2_values.append(value)
                    continue

                # OK, anything that is nullable or *after* a nullable arg needs to be here
                # TODO: We could try harder to avoid creating basic blocks in the common case
                new_seen_empty_reg = seen_empty_reg

                out = BasicBlock()
                if nullable:
                    # If this is the first nullable positional arg we've seen, create
                    # a register to track whether anything has been null.
                    # (We won't *check* the register until the next argument, though.)
                    if maybe_pos and not seen_empty_reg:
                        new_seen_empty_reg = Register(bool_rprimitive)
                        self.add(Assign(new_seen_empty_reg, self.false(), line))

                    skip = BasicBlock() if maybe_pos else out
                    keep = BasicBlock()
                    self.add(Branch(value, skip, keep, Branch.IS_ERROR))
                    self.activate_block(keep)

                # If this could be positional or named and we /might/ have seen a missing
                # positional arg, then we need to compile *both* a positional and named
                # version! What a pain!
                if maybe_pos and maybe_named and seen_empty_reg:
                    pos_block, named_block = BasicBlock(), BasicBlock()
                    self.add(Branch(seen_empty_reg, named_block, pos_block, Branch.BOOL))
                else:
                    pos_block = named_block = BasicBlock()
                    self.goto(pos_block)

                if maybe_pos:
                    self.activate_block(pos_block)
                    assert star_result
                    self.translate_special_method_call(
                        star_result, "append", [value], result_type=None, line=line
                    )
                    self.goto(out)

                if maybe_named and (not maybe_pos or seen_empty_reg):
                    self.activate_block(named_block)
                    assert name is not None
                    key = self.load_str(name)
                    assert star2_result
                    self.translate_special_method_call(
                        star2_result, "__setitem__", [key, value], result_type=None, line=line
                    )
                    self.goto(out)

                if nullable and maybe_pos and new_seen_empty_reg:
                    assert skip is not out
                    self.activate_block(skip)
                    self.add(Assign(new_seen_empty_reg, self.true(), line))
                    self.goto(out)

                self.activate_block(out)

                seen_empty_reg = new_seen_empty_reg

        assert not (star_result or star_values) or has_star
        assert not (star2_result or star2_values) or has_star2
        if has_star:
            # If we managed to make it this far without creating a
            # *args list, then we can directly create a
            # tuple. Otherwise create the tuple from the list.
            if star_result is None:
                star_result = self.new_tuple(star_values, line)
            else:
                star_result = self.primitive_op(list_tuple_op, [star_result], line)
        if has_star2 and star2_result is None:
            star2_result = self._create_dict(star2_keys, star2_values, line)

        return star_result, star2_result

    def py_call(
        self,
        function: Value,
        arg_values: list[Value],
        line: int,
        arg_kinds: list[ArgKind] | None = None,
        arg_names: Sequence[str | None] | None = None,
    ) -> Value:
        """Call a Python function (non-native and slow).

        Use py_call_op or py_call_with_kwargs_op for Python function call.
        """
        result = self._py_vector_call(function, arg_values, line, arg_kinds, arg_names)
        if result is not None:
            return result

        # If all arguments are positional, we can use py_call_op.
        if arg_kinds is None or all(kind == ARG_POS for kind in arg_kinds):
            return self.call_c(py_call_op, [function] + arg_values, line)

        # Otherwise fallback to py_call_with_kwargs_op.
        assert arg_names is not None

        pos_args_tuple, kw_args_dict = self._construct_varargs(
            list(zip(arg_values, arg_kinds, arg_names)), line, has_star=True, has_star2=True
        )
        assert pos_args_tuple and kw_args_dict

        return self.call_c(py_call_with_kwargs_op, [function, pos_args_tuple, kw_args_dict], line)

    def _py_vector_call(
        self,
        function: Value,
        arg_values: list[Value],
        line: int,
        arg_kinds: list[ArgKind] | None = None,
        arg_names: Sequence[str | None] | None = None,
    ) -> Value | None:
        """Call function using the vectorcall API if possible.

        Return the return value if successful. Return None if a non-vectorcall
        API should be used instead.
        """
        # We can do this if all args are positional or named (no *args or **kwargs, not optional).
        if arg_kinds is None or all(
            not kind.is_star() and not kind.is_optional() for kind in arg_kinds
        ):
            if arg_values:
                # Create a C array containing all arguments as boxed values.
                coerced_args = [self.coerce(arg, object_rprimitive, line) for arg in arg_values]
                arg_ptr = self.setup_rarray(object_rprimitive, coerced_args, object_ptr=True)
            else:
                arg_ptr = Integer(0, object_pointer_rprimitive)
            num_pos = num_positional_args(arg_values, arg_kinds)
            keywords = self._vectorcall_keywords(arg_names)
            value = self.call_c(
                py_vectorcall_op,
                [function, arg_ptr, Integer(num_pos, c_size_t_rprimitive), keywords],
                line,
            )
            if arg_values:
                # Make sure arguments won't be freed until after the call.
                # We need this because RArray doesn't support automatic
                # memory management.
                self.add(KeepAlive(coerced_args))
            return value
        return None

    def _vectorcall_keywords(self, arg_names: Sequence[str | None] | None) -> Value:
        """Return a reference to a tuple literal with keyword argument names.

        Return null pointer if there are no keyword arguments.
        """
        if arg_names:
            kw_list = [name for name in arg_names if name is not None]
            if kw_list:
                return self.add(LoadLiteral(tuple(kw_list), object_rprimitive))
        return Integer(0, object_rprimitive)

    def py_method_call(
        self,
        obj: Value,
        method_name: str,
        arg_values: list[Value],
        line: int,
        arg_kinds: list[ArgKind] | None,
        arg_names: Sequence[str | None] | None,
    ) -> Value:
        """Call a Python method (non-native and slow)."""
        result = self._py_vector_method_call(
            obj, method_name, arg_values, line, arg_kinds, arg_names
        )
        if result is not None:
            return result

        if arg_kinds is None or all(kind == ARG_POS for kind in arg_kinds):
            # Use legacy method call API
            method_name_reg = self.load_str(method_name)
            return self.call_c(py_method_call_op, [obj, method_name_reg] + arg_values, line)
        else:
            # Use py_call since it supports keyword arguments (and vectorcalls).
            method = self.py_get_attr(obj, method_name, line)
            return self.py_call(method, arg_values, line, arg_kinds=arg_kinds, arg_names=arg_names)

    def _py_vector_method_call(
        self,
        obj: Value,
        method_name: str,
        arg_values: list[Value],
        line: int,
        arg_kinds: list[ArgKind] | None,
        arg_names: Sequence[str | None] | None,
    ) -> Value | None:
        """Call method using the vectorcall API if possible.

        Return the return value if successful. Return None if a non-vectorcall
        API should be used instead.
        """
        if arg_kinds is None or all(
            not kind.is_star() and not kind.is_optional() for kind in arg_kinds
        ):
            method_name_reg = self.load_str(method_name)
            coerced_args = [
                self.coerce(arg, object_rprimitive, line) for arg in [obj] + arg_values
            ]
            arg_ptr = self.setup_rarray(object_rprimitive, coerced_args, object_ptr=True)
            num_pos = num_positional_args(arg_values, arg_kinds)
            keywords = self._vectorcall_keywords(arg_names)
            value = self.call_c(
                py_vectorcall_method_op,
                [
                    method_name_reg,
                    arg_ptr,
                    Integer((num_pos + 1) | PY_VECTORCALL_ARGUMENTS_OFFSET, c_size_t_rprimitive),
                    keywords,
                ],
                line,
            )
            # Make sure arguments won't be freed until after the call.
            # We need this because RArray doesn't support automatic
            # memory management.
            self.add(KeepAlive(coerced_args))
            return value
        return None

    def call(
        self,
        decl: FuncDecl,
        args: Sequence[Value],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None],
        line: int,
        *,
        bitmap_args: list[Register] | None = None,
    ) -> Value:
        """Call a native function.

        If bitmap_args is given, they override the values of (some) of the bitmap
        arguments used to track the presence of values for certain arguments. By
        default, the values of the bitmap arguments are inferred from args.
        """
        # Normalize args to positionals.
        args = self.native_args_to_positional(
            args, arg_kinds, arg_names, decl.sig, line, bitmap_args=bitmap_args
        )
        return self.add(Call(decl, args, line))

    def native_args_to_positional(
        self,
        args: Sequence[Value],
        arg_kinds: list[ArgKind],
        arg_names: Sequence[str | None],
        sig: FuncSignature,
        line: int,
        *,
        bitmap_args: list[Register] | None = None,
    ) -> list[Value]:
        """Prepare arguments for a native call.

        Given args/kinds/names and a target signature for a native call, map
        keyword arguments to their appropriate place in the argument list,
        fill in error values for unspecified default arguments,
        package arguments that will go into *args/**kwargs into a tuple/dict,
        and coerce arguments to the appropriate type.
        """

        sig_args = sig.args
        n = sig.num_bitmap_args
        if n:
            sig_args = sig_args[:-n]

        sig_arg_kinds = [arg.kind for arg in sig_args]
        sig_arg_names = [arg.name for arg in sig_args]

        concrete_kinds = [concrete_arg_kind(arg_kind) for arg_kind in arg_kinds]
        formal_to_actual = map_actuals_to_formals(
            concrete_kinds,
            arg_names,
            sig_arg_kinds,
            sig_arg_names,
            lambda n: AnyType(TypeOfAny.special_form),
        )

        # First scan for */** and construct those
        has_star = has_star2 = False
        star_arg_entries = []
        for lst, arg in zip(formal_to_actual, sig_args):
            if arg.kind.is_star():
                star_arg_entries.extend([(args[i], arg_kinds[i], arg_names[i]) for i in lst])
            has_star = has_star or arg.kind == ARG_STAR
            has_star2 = has_star2 or arg.kind == ARG_STAR2

        star_arg, star2_arg = self._construct_varargs(
            star_arg_entries, line, has_star=has_star, has_star2=has_star2
        )

        # Flatten out the arguments, loading error values for default
        # arguments, constructing tuples/dicts for star args, and
        # coercing everything to the expected type.
        output_args: list[Value] = []
        for lst, arg in zip(formal_to_actual, sig_args):
            if arg.kind == ARG_STAR:
                assert star_arg
                output_arg = star_arg
            elif arg.kind == ARG_STAR2:
                assert star2_arg
                output_arg = star2_arg
            elif not lst:
                if is_fixed_width_rtype(arg.type):
                    output_arg = Integer(0, arg.type)
                elif is_float_rprimitive(arg.type):
                    output_arg = Float(0.0)
                else:
                    output_arg = self.add(LoadErrorValue(arg.type, is_borrowed=True))
            else:
                base_arg = args[lst[0]]

                if arg_kinds[lst[0]].is_optional():
                    output_arg = self.coerce_nullable(base_arg, arg.type, line)
                else:
                    output_arg = self.coerce(base_arg, arg.type, line)

            output_args.append(output_arg)

        for i in reversed(range(n)):
            if bitmap_args and i < len(bitmap_args):
                # Use override provided by caller
                output_args.append(bitmap_args[i])
                continue
            # Infer values of bitmap args
            bitmap = 0
            c = 0
            for lst, arg in zip(formal_to_actual, sig_args):
                if arg.kind.is_optional() and arg.type.error_overlap:
                    if i * BITMAP_BITS <= c < (i + 1) * BITMAP_BITS:
                        if lst:
                            bitmap |= 1 << (c & (BITMAP_BITS - 1))
                    c += 1
            output_args.append(Integer(bitmap, bitmap_rprimitive))

        return output_args

    def gen_method_call(
        self,
        base: Value,
        name: str,
        arg_values: list[Value],
        result_type: RType | None,
        line: int,
        arg_kinds: list[ArgKind] | None = None,
        arg_names: list[str | None] | None = None,
        can_borrow: bool = False,
    ) -> Value:
        """Generate either a native or Python method call."""
        # If we have *args, then fallback to Python method call.
        if arg_kinds is not None and any(kind.is_star() for kind in arg_kinds):
            return self.py_method_call(base, name, arg_values, line, arg_kinds, arg_names)

        # If the base type is one of ours, do a MethodCall
        if (
            isinstance(base.type, RInstance)
            and base.type.class_ir.is_ext_class
            and not base.type.class_ir.builtin_base
        ):
            if base.type.class_ir.has_method(name):
                decl = base.type.class_ir.method_decl(name)
                if arg_kinds is None:
                    assert arg_names is None, "arg_kinds not present but arg_names is"
                    arg_kinds = [ARG_POS for _ in arg_values]
                    arg_names = [None for _ in arg_values]
                else:
                    assert arg_names is not None, "arg_kinds present but arg_names is not"

                # Normalize args to positionals.
                assert decl.bound_sig
                arg_values = self.native_args_to_positional(
                    arg_values, arg_kinds, arg_names, decl.bound_sig, line
                )
                return self.add(MethodCall(base, name, arg_values, line))
            elif base.type.class_ir.has_attr(name):
                function = self.add(GetAttr(base, name, line))
                return self.py_call(
                    function, arg_values, line, arg_kinds=arg_kinds, arg_names=arg_names
                )

        elif isinstance(base.type, RUnion):
            return self.union_method_call(
                base, base.type, name, arg_values, result_type, line, arg_kinds, arg_names
            )

        # Try to do a special-cased method call
        if not arg_kinds or arg_kinds == [ARG_POS] * len(arg_values):
            target = self.translate_special_method_call(
                base, name, arg_values, result_type, line, can_borrow=can_borrow
            )
            if target:
                return target

        # Fall back to Python method call
        return self.py_method_call(base, name, arg_values, line, arg_kinds, arg_names)

    def union_method_call(
        self,
        base: Value,
        obj_type: RUnion,
        name: str,
        arg_values: list[Value],
        return_rtype: RType | None,
        line: int,
        arg_kinds: list[ArgKind] | None,
        arg_names: list[str | None] | None,
    ) -> Value:
        """Generate a method call with a union type for the object."""
        # Union method call needs a return_rtype for the type of the output register.
        # If we don't have one, use object_rprimitive.
        return_rtype = return_rtype or object_rprimitive

        def call_union_item(value: Value) -> Value:
            return self.gen_method_call(
                value, name, arg_values, return_rtype, line, arg_kinds, arg_names
            )

        return self.decompose_union_helper(base, obj_type, return_rtype, call_union_item, line)

    # Loading various values

    def none(self) -> Value:
        """Load unboxed None value (type: none_rprimitive)."""
        return Integer(1, none_rprimitive)

    def true(self) -> Value:
        """Load unboxed True value (type: bool_rprimitive)."""
        return Integer(1, bool_rprimitive)

    def false(self) -> Value:
        """Load unboxed False value (type: bool_rprimitive)."""
        return Integer(0, bool_rprimitive)

    def none_object(self) -> Value:
        """Load Python None value (type: object_rprimitive)."""
        return self.add(LoadAddress(none_object_op.type, none_object_op.src, line=-1))

    def load_int(self, value: int) -> Value:
        """Load a tagged (Python) integer literal value."""
        if value > MAX_LITERAL_SHORT_INT or value < MIN_LITERAL_SHORT_INT:
            return self.add(LoadLiteral(value, int_rprimitive))
        else:
            return Integer(value)

    def load_float(self, value: float) -> Value:
        """Load a float literal value."""
        return Float(value)

    def load_str(self, value: str) -> Value:
        """Load a str literal value.

        This is useful for more than just str literals; for example, method calls
        also require a PyObject * form for the name of the method.
        """
        return self.add(LoadLiteral(value, str_rprimitive))

    def load_bytes(self, value: bytes) -> Value:
        """Load a bytes literal value."""
        return self.add(LoadLiteral(value, bytes_rprimitive))

    def load_complex(self, value: complex) -> Value:
        """Load a complex literal value."""
        return self.add(LoadLiteral(value, object_rprimitive))

    def load_static_checked(
        self,
        typ: RType,
        identifier: str,
        module_name: str | None = None,
        namespace: str = NAMESPACE_STATIC,
        line: int = -1,
        error_msg: str | None = None,
    ) -> Value:
        if error_msg is None:
            error_msg = f'name "{identifier}" is not defined'
        ok_block, error_block = BasicBlock(), BasicBlock()
        value = self.add(LoadStatic(typ, identifier, module_name, namespace, line=line))
        self.add(Branch(value, error_block, ok_block, Branch.IS_ERROR, rare=True))
        self.activate_block(error_block)
        self.add(RaiseStandardError(RaiseStandardError.NAME_ERROR, error_msg, line))
        self.add(Unreachable())
        self.activate_block(ok_block)
        return value

    def load_module(self, name: str) -> Value:
        return self.add(LoadStatic(object_rprimitive, name, namespace=NAMESPACE_MODULE))

    def get_native_type(self, cls: ClassIR) -> Value:
        """Load native type object."""
        fullname = f"{cls.module_name}.{cls.name}"
        return self.load_native_type_object(fullname)

    def load_native_type_object(self, fullname: str) -> Value:
        module, name = fullname.rsplit(".", 1)
        return self.add(LoadStatic(object_rprimitive, name, module, NAMESPACE_TYPE))

    # Other primitive operations

    def binary_op(self, lreg: Value, rreg: Value, op: str, line: int) -> Value:
        """Perform a binary operation.

        Generate specialized operations based on operand types, with a fallback
        to generic operations.
        """
        ltype = lreg.type
        rtype = rreg.type

        # Special case tuple comparison here so that nested tuples can be supported
        if isinstance(ltype, RTuple) and isinstance(rtype, RTuple) and op in ("==", "!="):
            return self.compare_tuples(lreg, rreg, op, line)

        # Special case == and != when we can resolve the method call statically
        if op in ("==", "!="):
            value = self.translate_eq_cmp(lreg, rreg, op, line)
            if value is not None:
                return value

        # Special case various ops
        if op in ("is", "is not"):
            return self.translate_is_op(lreg, rreg, op, line)
        # TODO: modify 'str' to use same interface as 'compare_bytes' as it avoids
        # call to PyErr_Occurred()
        if is_str_rprimitive(ltype) and is_str_rprimitive(rtype) and op in ("==", "!="):
            return self.compare_strings(lreg, rreg, op, line)
        if is_bytes_rprimitive(ltype) and is_bytes_rprimitive(rtype) and op in ("==", "!="):
            return self.compare_bytes(lreg, rreg, op, line)
        if (
            is_bool_or_bit_rprimitive(ltype)
            and is_bool_or_bit_rprimitive(rtype)
            and op in BOOL_BINARY_OPS
        ):
            if op in ComparisonOp.signed_ops:
                return self.bool_comparison_op(lreg, rreg, op, line)
            else:
                return self.bool_bitwise_op(lreg, rreg, op[0], line)
        if isinstance(rtype, RInstance) and op in ("in", "not in"):
            return self.translate_instance_contains(rreg, lreg, op, line)
        if is_fixed_width_rtype(ltype):
            if op in FIXED_WIDTH_INT_BINARY_OPS:
                op = op.removesuffix("=")
                if op != "//":
                    op_id = int_op_to_id[op]
                else:
                    op_id = IntOp.DIV
                if is_bool_or_bit_rprimitive(rtype):
                    rreg = self.coerce(rreg, ltype, line)
                    rtype = ltype
                if is_fixed_width_rtype(rtype) or is_tagged(rtype):
                    return self.fixed_width_int_op(ltype, lreg, rreg, op_id, line)
                if isinstance(rreg, Integer):
                    return self.fixed_width_int_op(
                        ltype, lreg, self.coerce(rreg, ltype, line), op_id, line
                    )
            elif op in ComparisonOp.signed_ops:
                if is_int_rprimitive(rtype):
                    rreg = self.coerce_int_to_fixed_width(rreg, ltype, line)
                elif is_bool_or_bit_rprimitive(rtype):
                    rreg = self.coerce(rreg, ltype, line)
                op_id = ComparisonOp.signed_ops[op]
                if is_fixed_width_rtype(rreg.type):
                    return self.comparison_op(lreg, rreg, op_id, line)
                if isinstance(rreg, Integer):
                    return self.comparison_op(lreg, self.coerce(rreg, ltype, line), op_id, line)
        elif is_fixed_width_rtype(rtype):
            if op in FIXED_WIDTH_INT_BINARY_OPS:
                op = op.removesuffix("=")
                if op != "//":
                    op_id = int_op_to_id[op]
                else:
                    op_id = IntOp.DIV
                if isinstance(lreg, Integer):
                    return self.fixed_width_int_op(
                        rtype, self.coerce(lreg, rtype, line), rreg, op_id, line
                    )
                if is_tagged(ltype):
                    return self.fixed_width_int_op(rtype, lreg, rreg, op_id, line)
                if is_bool_or_bit_rprimitive(ltype):
                    lreg = self.coerce(lreg, rtype, line)
                    return self.fixed_width_int_op(rtype, lreg, rreg, op_id, line)
            elif op in ComparisonOp.signed_ops:
                if is_int_rprimitive(ltype):
                    lreg = self.coerce_int_to_fixed_width(lreg, rtype, line)
                elif is_bool_or_bit_rprimitive(ltype):
                    lreg = self.coerce(lreg, rtype, line)
                op_id = ComparisonOp.signed_ops[op]
                if isinstance(lreg, Integer):
                    return self.comparison_op(self.coerce(lreg, rtype, line), rreg, op_id, line)
                if is_fixed_width_rtype(lreg.type):
                    return self.comparison_op(lreg, rreg, op_id, line)

        if is_float_rprimitive(ltype) or is_float_rprimitive(rtype):
            if isinstance(lreg, Integer):
                lreg = Float(float(lreg.numeric_value()))
            elif isinstance(rreg, Integer):
                rreg = Float(float(rreg.numeric_value()))
            elif is_int_rprimitive(lreg.type):
                lreg = self.int_to_float(lreg, line)
            elif is_int_rprimitive(rreg.type):
                rreg = self.int_to_float(rreg, line)
            if is_float_rprimitive(lreg.type) and is_float_rprimitive(rreg.type):
                if op in float_comparison_op_to_id:
                    return self.compare_floats(lreg, rreg, float_comparison_op_to_id[op], line)
                if op.endswith("="):
                    base_op = op[:-1]
                else:
                    base_op = op
                if base_op in float_op_to_id:
                    return self.float_op(lreg, rreg, base_op, line)

        dunder_op = self.dunder_op(lreg, rreg, op, line)
        if dunder_op:
            return dunder_op

        primitive_ops_candidates = binary_ops.get(op, [])
        target = self.matching_primitive_op(primitive_ops_candidates, [lreg, rreg], line)
        assert target, "Unsupported binary operation: %s" % op
        return target

    def dunder_op(self, lreg: Value, rreg: Value | None, op: str, line: int) -> Value | None:
        """
        Dispatch a dunder method if applicable.
        For example for `a + b` it will use `a.__add__(b)` which can lead to higher performance
        due to the fact that the method could be already compiled and optimized instead of going
        all the way through `PyNumber_Add(a, b)` python api (making a jump into the python DL).
        """
        ltype = lreg.type
        if not isinstance(ltype, RInstance):
            return None

        method_name = op_methods.get(op) if rreg else unary_op_methods.get(op)
        if method_name is None:
            return None

        if not ltype.class_ir.has_method(method_name):
            return None

        decl = ltype.class_ir.method_decl(method_name)
        if not rreg and len(decl.sig.args) != 1:
            return None

        if rreg and (len(decl.sig.args) != 2 or not is_subtype(rreg.type, decl.sig.args[1].type)):
            return None

        if rreg and is_subtype(not_implemented_op.type, decl.sig.ret_type):
            # If the method is able to return NotImplemented, we should not optimize it.
            # We can just let go so it will be handled through the python api.
            return None

        args = [rreg] if rreg else []
        return self.gen_method_call(lreg, method_name, args, decl.sig.ret_type, line)

    def check_tagged_short_int(self, val: Value, line: int, negated: bool = False) -> Value:
        """Check if a tagged integer is a short integer.

        Return the result of the check (value of type 'bit').
        """
        int_tag = Integer(1, c_pyssize_t_rprimitive, line)
        bitwise_and = self.int_op(c_pyssize_t_rprimitive, val, int_tag, IntOp.AND, line)
        zero = Integer(0, c_pyssize_t_rprimitive, line)
        op = ComparisonOp.NEQ if negated else ComparisonOp.EQ
        check = self.comparison_op(bitwise_and, zero, op, line)
        return check

    def compare_strings(self, lhs: Value, rhs: Value, op: str, line: int) -> Value:
        """Compare two strings"""
        if op == "==":
            return self.primitive_op(str_eq, [lhs, rhs], line)
        elif op == "!=":
            eq = self.primitive_op(str_eq, [lhs, rhs], line)
            return self.add(ComparisonOp(eq, self.false(), ComparisonOp.EQ, line))
        compare_result = self.call_c(unicode_compare, [lhs, rhs], line)
        error_constant = Integer(-1, c_int_rprimitive, line)
        compare_error_check = self.add(
            ComparisonOp(compare_result, error_constant, ComparisonOp.EQ, line)
        )
        exception_check, propagate, final_compare = BasicBlock(), BasicBlock(), BasicBlock()
        branch = Branch(compare_error_check, exception_check, final_compare, Branch.BOOL)
        branch.negated = False
        self.add(branch)
        self.activate_block(exception_check)
        check_error_result = self.call_c(err_occurred_op, [], line)
        null = Integer(0, pointer_rprimitive, line)
        compare_error_check = self.add(
            ComparisonOp(check_error_result, null, ComparisonOp.NEQ, line)
        )
        branch = Branch(compare_error_check, propagate, final_compare, Branch.BOOL)
        branch.negated = False
        self.add(branch)
        self.activate_block(propagate)
        self.call_c(keep_propagating_op, [], line)
        self.goto(final_compare)
        self.activate_block(final_compare)
        op_type = ComparisonOp.EQ if op == "==" else ComparisonOp.NEQ
        return self.add(ComparisonOp(compare_result, Integer(0, c_int_rprimitive), op_type, line))

    def compare_bytes(self, lhs: Value, rhs: Value, op: str, line: int) -> Value:
        compare_result = self.call_c(bytes_compare, [lhs, rhs], line)
        op_type = ComparisonOp.EQ if op == "==" else ComparisonOp.NEQ
        return self.add(ComparisonOp(compare_result, Integer(1, c_int_rprimitive), op_type, line))

    def compare_tuples(self, lhs: Value, rhs: Value, op: str, line: int = -1) -> Value:
        """Compare two tuples item by item"""
        # type cast to pass mypy check
        assert isinstance(lhs.type, RTuple) and isinstance(rhs.type, RTuple), (lhs.type, rhs.type)
        equal = True if op == "==" else False
        result = Register(bool_rprimitive)
        # tuples of different lengths
        if len(lhs.type.types) != len(rhs.type.types):
            self.add(Assign(result, self.false() if equal else self.true(), line))
            return result
        # empty tuples
        if len(lhs.type.types) == 0 and len(rhs.type.types) == 0:
            self.add(Assign(result, self.true() if equal else self.false(), line))
            return result
        length = len(lhs.type.types)
        false_assign, true_assign, out = BasicBlock(), BasicBlock(), BasicBlock()
        check_blocks = [BasicBlock() for _ in range(length)]
        lhs_items = [self.add(TupleGet(lhs, i, line)) for i in range(length)]
        rhs_items = [self.add(TupleGet(rhs, i, line)) for i in range(length)]

        if equal:
            early_stop, final = false_assign, true_assign
        else:
            early_stop, final = true_assign, false_assign

        for i in range(len(lhs.type.types)):
            if i != 0:
                self.activate_block(check_blocks[i])
            lhs_item = lhs_items[i]
            rhs_item = rhs_items[i]
            compare = self.binary_op(lhs_item, rhs_item, op, line)
            # Cast to bool if necessary since most types uses comparison returning a object type
            # See generic_ops.py for more information
            if not is_bool_or_bit_rprimitive(compare.type):
                compare = self.primitive_op(bool_op, [compare], line)
            if i < len(lhs.type.types) - 1:
                branch = Branch(compare, early_stop, check_blocks[i + 1], Branch.BOOL)
            else:
                branch = Branch(compare, early_stop, final, Branch.BOOL)
            # if op is ==, we branch on false, else branch on true
            branch.negated = equal
            self.add(branch)
        self.activate_block(false_assign)
        self.add(Assign(result, self.false(), line))
        self.goto(out)
        self.activate_block(true_assign)
        self.add(Assign(result, self.true(), line))
        self.goto_and_activate(out)
        return result

    def translate_instance_contains(self, inst: Value, item: Value, op: str, line: int) -> Value:
        res = self.gen_method_call(inst, "__contains__", [item], None, line)
        if not is_bool_or_bit_rprimitive(res.type):
            res = self.primitive_op(bool_op, [res], line)
        if op == "not in":
            res = self.bool_bitwise_op(res, Integer(1, rtype=bool_rprimitive), "^", line)
        return res

    def bool_bitwise_op(self, lreg: Value, rreg: Value, op: str, line: int) -> Value:
        if op == "&":
            code = IntOp.AND
        elif op == "|":
            code = IntOp.OR
        elif op == "^":
            code = IntOp.XOR
        else:
            assert False, op
        return self.add(IntOp(bool_rprimitive, lreg, rreg, code, line))

    def bool_comparison_op(self, lreg: Value, rreg: Value, op: str, line: int) -> Value:
        op_id = ComparisonOp.signed_ops[op]
        return self.comparison_op(lreg, rreg, op_id, line)

    def unary_not(self, value: Value, line: int) -> Value:
        mask = Integer(1, value.type, line)
        return self.int_op(value.type, value, mask, IntOp.XOR, line)

    def unary_op(self, value: Value, expr_op: str, line: int) -> Value:
        typ = value.type
        if is_bool_or_bit_rprimitive(typ):
            if expr_op == "not":
                return self.unary_not(value, line)
            if expr_op == "+":
                return value
        if is_fixed_width_rtype(typ):
            if expr_op == "-":
                # Translate to '0 - x'
                return self.int_op(typ, Integer(0, typ), value, IntOp.SUB, line)
            elif expr_op == "~":
                if typ.is_signed:
                    # Translate to 'x ^ -1'
                    return self.int_op(typ, value, Integer(-1, typ), IntOp.XOR, line)
                else:
                    # Translate to 'x ^ 0xff...'
                    mask = (1 << (typ.size * 8)) - 1
                    return self.int_op(typ, value, Integer(mask, typ), IntOp.XOR, line)
            elif expr_op == "+":
                return value
        if is_float_rprimitive(typ):
            if expr_op == "-":
                return self.add(FloatNeg(value, line))
            elif expr_op == "+":
                return value

        if isinstance(value, Integer):
            # TODO: Overflow? Unsigned?
            num = value.value
            if is_short_int_rprimitive(typ):
                num >>= 1
            return Integer(-num, typ, value.line)
        if is_tagged(typ) and expr_op == "+":
            return value
        if isinstance(value, Float):
            return Float(-value.value, value.line)
        if isinstance(typ, RInstance):
            result = self.dunder_op(value, None, expr_op, line)
            if result is not None:
                return result
        primitive_ops_candidates = unary_ops.get(expr_op, [])
        target = self.matching_primitive_op(primitive_ops_candidates, [value], line)
        assert target, "Unsupported unary operation: %s" % expr_op
        return target

    def make_dict(self, key_value_pairs: Sequence[DictEntry], line: int) -> Value:
        result: Value | None = None
        keys: list[Value] = []
        values: list[Value] = []
        for key, value in key_value_pairs:
            if key is not None:
                # key:value
                if result is None:
                    keys.append(key)
                    values.append(value)
                    continue

                self.translate_special_method_call(
                    result, "__setitem__", [key, value], result_type=None, line=line
                )
            else:
                # **value
                if result is None:
                    result = self._create_dict(keys, values, line)

                self.call_c(dict_update_in_display_op, [result, value], line=line)

        if result is None:
            result = self._create_dict(keys, values, line)

        return result

    def new_list_op_with_length(self, length: Value, line: int) -> Value:
        """This function returns an uninitialized list.

        If the length is non-zero, the caller must initialize the list, before
        it can be made visible to user code -- otherwise the list object is broken.
        You might need further initialization with `new_list_set_item_op` op.

        Args:
            length: desired length of the new list. The rtype should be
                    c_pyssize_t_rprimitive
            line: line number
        """
        return self.call_c(new_list_op, [length], line)

    def new_list_op(self, values: list[Value], line: int) -> Value:
        length: list[Value] = [Integer(len(values), c_pyssize_t_rprimitive, line)]
        if len(values) >= LIST_BUILDING_EXPANSION_THRESHOLD:
            return self.call_c(list_build_op, length + values, line)

        # If the length of the list is less than the threshold,
        # LIST_BUILDING_EXPANSION_THRESHOLD, we directly expand the
        # for-loop and inline the SetMem operation, which is faster
        # than list_build_op, however generates more code.
        result_list = self.call_c(new_list_op, length, line)
        if not values:
            return result_list
        args = [self.coerce(item, object_rprimitive, line) for item in values]
        ob_item_base = self.add(PrimitiveOp([result_list], list_items, line))
        for i in range(len(values)):
            self.primitive_op(
                buf_init_item, [ob_item_base, Integer(i, c_pyssize_t_rprimitive), args[i]], line
            )
        self.add(KeepAlive([result_list]))
        return result_list

    def new_set_op(self, values: list[Value], line: int) -> Value:
        return self.primitive_op(new_set_op, values, line)

    def setup_rarray(
        self, item_type: RType, values: Sequence[Value], *, object_ptr: bool = False
    ) -> Value:
        """Declare and initialize a new RArray, returning its address."""
        array = Register(RArray(item_type, len(values)))
        self.add(AssignMulti(array, list(values)))
        return self.add(
            LoadAddress(object_pointer_rprimitive if object_ptr else c_pointer_rprimitive, array)
        )

    def shortcircuit_helper(
        self,
        op: str,
        expr_type: RType,
        left: Callable[[], Value],
        right: Callable[[], Value],
        line: int,
    ) -> Value:
        # Having actual Phi nodes would be really nice here!
        target = Register(expr_type)
        # left_body takes the value of the left side, right_body the right
        left_body, right_body, next_block = BasicBlock(), BasicBlock(), BasicBlock()
        # true_body is taken if the left is true, false_body if it is false.
        # For 'and' the value is the right side if the left is true, and for 'or'
        # it is the right side if the left is false.
        true_body, false_body = (right_body, left_body) if op == "and" else (left_body, right_body)

        left_value = left()
        self.add_bool_branch(left_value, true_body, false_body)

        self.activate_block(left_body)
        left_coerced = self.coerce(left_value, expr_type, line)
        self.add(Assign(target, left_coerced))
        self.goto(next_block)

        self.activate_block(right_body)
        right_value = right()
        right_coerced = self.coerce(right_value, expr_type, line)
        self.add(Assign(target, right_coerced))
        self.goto(next_block)

        self.activate_block(next_block)
        return target

    def bool_value(self, value: Value) -> Value:
        """Return bool(value).

        The result type can be bit_rprimitive or bool_rprimitive.
        """
        if is_bool_or_bit_rprimitive(value.type):
            result = value
        elif is_runtime_subtype(value.type, int_rprimitive):
            zero = Integer(0, short_int_rprimitive)
            result = self.comparison_op(value, zero, ComparisonOp.NEQ, value.line)
        elif is_fixed_width_rtype(value.type):
            zero = Integer(0, value.type)
            result = self.add(ComparisonOp(value, zero, ComparisonOp.NEQ))
        elif is_same_type(value.type, str_rprimitive):
            result = self.call_c(str_check_if_true, [value], value.line)
        elif is_same_type(value.type, list_rprimitive) or is_same_type(
            value.type, dict_rprimitive
        ):
            length = self.builtin_len(value, value.line)
            zero = Integer(0)
            result = self.binary_op(length, zero, "!=", value.line)
        elif (
            isinstance(value.type, RInstance)
            and value.type.class_ir.is_ext_class
            and value.type.class_ir.has_method("__bool__")
        ):
            # Directly call the __bool__ method on classes that have it.
            result = self.gen_method_call(value, "__bool__", [], bool_rprimitive, value.line)
        elif is_float_rprimitive(value.type):
            result = self.compare_floats(value, Float(0.0), FloatComparisonOp.NEQ, value.line)
        else:
            value_type = optional_value_type(value.type)
            if value_type is not None:
                not_none = self.translate_is_op(value, self.none_object(), "is not", value.line)
                always_truthy = False
                if isinstance(value_type, RInstance):
                    # check whether X.__bool__ is always just the default (object.__bool__)
                    if not value_type.class_ir.has_method(
                        "__bool__"
                    ) and value_type.class_ir.is_method_final("__bool__"):
                        always_truthy = True

                if always_truthy:
                    result = not_none
                else:
                    # "X | None" where X may be falsey and requires a check
                    result = Register(bit_rprimitive)
                    true, false, end = BasicBlock(), BasicBlock(), BasicBlock()
                    branch = Branch(not_none, true, false, Branch.BOOL)
                    self.add(branch)
                    self.activate_block(true)
                    # unbox_or_cast instead of coerce because we want the
                    # type to change even if it is a subtype.
                    remaining = self.unbox_or_cast(value, value_type, value.line)
                    as_bool = self.bool_value(remaining)
                    self.add(Assign(result, as_bool))
                    self.goto(end)
                    self.activate_block(false)
                    self.add(Assign(result, Integer(0, bit_rprimitive)))
                    self.goto(end)
                    self.activate_block(end)
            else:
                result = self.primitive_op(bool_op, [value], value.line)
        return result

    def add_bool_branch(self, value: Value, true: BasicBlock, false: BasicBlock) -> None:
        opt_value_type = optional_value_type(value.type)
        if opt_value_type is None:
            bool_value = self.bool_value(value)
            self.add(Branch(bool_value, true, false, Branch.BOOL))
        else:
            # Special-case optional types
            is_none = self.translate_is_op(value, self.none_object(), "is not", value.line)
            branch = Branch(is_none, true, false, Branch.BOOL)
            self.add(branch)
            always_truthy = False
            if isinstance(opt_value_type, RInstance):
                # check whether X.__bool__ is always just the default (object.__bool__)
                if not opt_value_type.class_ir.has_method(
                    "__bool__"
                ) and opt_value_type.class_ir.is_method_final("__bool__"):
                    always_truthy = True

            if not always_truthy:
                # Optional[X] where X may be falsey and requires a check
                branch.true = BasicBlock()
                self.activate_block(branch.true)
                # unbox_or_cast instead of coerce because we want the
                # type to change even if it is a subtype.
                remaining = self.unbox_or_cast(value, opt_value_type, value.line)
                self.add_bool_branch(remaining, true, false)

    def call_c(
        self,
        desc: CFunctionDescription,
        args: list[Value],
        line: int,
        result_type: RType | None = None,
    ) -> Value:
        """Call function using C/native calling convention (not a Python callable)."""
        # Handle void function via singleton RVoid instance
        coerced = []
        # Coerce fixed number arguments
        for i in range(min(len(args), len(desc.arg_types))):
            formal_type = desc.arg_types[i]
            arg = args[i]
            arg = self.coerce(arg, formal_type, line)
            coerced.append(arg)
        # Reorder args if necessary
        if desc.ordering is not None:
            assert desc.var_arg_type is None
            coerced = [coerced[i] for i in desc.ordering]
        # Coerce any var_arg
        var_arg_idx = -1
        if desc.var_arg_type is not None:
            var_arg_idx = len(desc.arg_types)
            for i in range(len(desc.arg_types), len(args)):
                arg = args[i]
                arg = self.coerce(arg, desc.var_arg_type, line)
                coerced.append(arg)
        # Add extra integer constant if any
        for item in desc.extra_int_constants:
            val, typ = item
            extra_int_constant = Integer(val, typ, line)
            coerced.append(extra_int_constant)
        error_kind = desc.error_kind
        if error_kind == ERR_NEG_INT:
            # Handled with an explicit comparison
            error_kind = ERR_NEVER
        target = self.add(
            CallC(
                desc.c_function_name,
                coerced,
                desc.return_type,
                desc.steals,
                desc.is_borrowed,
                error_kind,
                line,
                var_arg_idx,
                is_pure=desc.is_pure,
            )
        )
        if desc.is_borrowed:
            # If the result is borrowed, force the arguments to be
            # kept alive afterwards, as otherwise the result might be
            # immediately freed, at the risk of a dangling pointer.
            for arg in coerced:
                if not isinstance(arg, (Integer, LoadLiteral)):
                    self.keep_alives.append(arg)
        if desc.error_kind == ERR_NEG_INT:
            comp = ComparisonOp(target, Integer(0, desc.return_type, line), ComparisonOp.SGE, line)
            comp.error_kind = ERR_FALSE
            self.add(comp)

        if desc.truncated_type is None:
            result = target
        else:
            truncate = self.add(Truncate(target, desc.truncated_type))
            result = truncate
        if result_type and not is_runtime_subtype(result.type, result_type):
            if is_none_rprimitive(result_type):
                # Special case None return. The actual result may actually be a bool
                # and so we can't just coerce it.
                result = self.none()
            else:
                result = self.coerce(target, result_type, line, can_borrow=desc.is_borrowed)
        return result

    def matching_call_c(
        self,
        candidates: list[CFunctionDescription],
        args: list[Value],
        line: int,
        result_type: RType | None = None,
        can_borrow: bool = False,
    ) -> Value | None:
        matching: CFunctionDescription | None = None
        for desc in candidates:
            if len(desc.arg_types) != len(args):
                continue
            if all(
                is_subtype(actual.type, formal) for actual, formal in zip(args, desc.arg_types)
            ) and (not desc.is_borrowed or can_borrow):
                if matching:
                    assert matching.priority != desc.priority, "Ambiguous:\n1) {}\n2) {}".format(
                        matching, desc
                    )
                    if desc.priority > matching.priority:
                        matching = desc
                else:
                    matching = desc
        if matching:
            target = self.call_c(matching, args, line, result_type)
            return target
        return None

    def primitive_op(
        self,
        desc: PrimitiveDescription,
        args: list[Value],
        line: int,
        result_type: RType | None = None,
    ) -> Value:
        """Add a primitive op."""
        # Does this primitive map into calling a Python C API
        # or an internal mypyc C API function?
        if desc.c_function_name:
            # TODO: Generate PrimitiveOps here and transform them into CallC
            # ops only later in the lowering pass
            c_desc = CFunctionDescription(
                desc.name,
                desc.arg_types,
                desc.return_type,
                desc.var_arg_type,
                desc.truncated_type,
                desc.c_function_name,
                desc.error_kind,
                desc.steals,
                desc.is_borrowed,
                desc.ordering,
                desc.extra_int_constants,
                desc.priority,
                is_pure=desc.is_pure,
            )
            return self.call_c(c_desc, args, line, result_type=result_type)

        # This primitive gets transformed in a lowering pass to
        # lower-level IR ops using a custom transform function.

        coerced = []
        # Coerce fixed number arguments
        for i in range(min(len(args), len(desc.arg_types))):
            formal_type = desc.arg_types[i]
            arg = args[i]
            assert formal_type is not None  # TODO
            arg = self.coerce(arg, formal_type, line)
            coerced.append(arg)
        assert desc.ordering is None
        assert desc.var_arg_type is None
        assert not desc.extra_int_constants
        target = self.add(PrimitiveOp(coerced, desc, line=line))
        if desc.is_borrowed:
            # If the result is borrowed, force the arguments to be
            # kept alive afterwards, as otherwise the result might be
            # immediately freed, at the risk of a dangling pointer.
            for arg in coerced:
                if not isinstance(arg, (Integer, LoadLiteral)):
                    self.keep_alives.append(arg)
        if desc.error_kind == ERR_NEG_INT:
            comp = ComparisonOp(target, Integer(0, desc.return_type, line), ComparisonOp.SGE, line)
            comp.error_kind = ERR_FALSE
            self.add(comp)

        assert desc.truncated_type is None
        result = target
        if result_type and not is_runtime_subtype(result.type, result_type):
            if is_none_rprimitive(result_type):
                # Special case None return. The actual result may actually be a bool
                # and so we can't just coerce it.
                result = self.none()
            else:
                result = self.coerce(result, result_type, line, can_borrow=desc.is_borrowed)
        return result

    def matching_primitive_op(
        self,
        candidates: list[PrimitiveDescription],
        args: list[Value],
        line: int,
        result_type: RType | None = None,
        can_borrow: bool = False,
    ) -> Value | None:
        matching: PrimitiveDescription | None = None
        for desc in candidates:
            if len(desc.arg_types) != len(args):
                continue
            if all(
                # formal is not None and # TODO
                is_subtype(actual.type, formal)
                for actual, formal in zip(args, desc.arg_types)
            ) and (not desc.is_borrowed or can_borrow):
                if matching:
                    assert matching.priority != desc.priority, "Ambiguous:\n1) {}\n2) {}".format(
                        matching, desc
                    )
                    if desc.priority > matching.priority:
                        matching = desc
                else:
                    matching = desc
        if matching:
            return self.primitive_op(matching, args, line=line, result_type=result_type)
        return None

    def int_op(self, type: RType, lhs: Value, rhs: Value, op: int, line: int = -1) -> Value:
        """Generate a native integer binary op.

        Use native/C semantics, which sometimes differ from Python
        semantics.

        Args:
            type: Either int64_rprimitive or int32_rprimitive
            op: IntOp.* constant (e.g. IntOp.ADD)
        """
        return self.add(IntOp(type, lhs, rhs, op, line))

    def float_op(self, lhs: Value, rhs: Value, op: str, line: int) -> Value:
        """Generate a native float binary arithmetic operation.

        This follows Python semantics (e.g. raise exception on division by zero).
        Add a FloatOp directly if you want low-level semantics.

        Args:
            op: Binary operator (e.g. '+' or '*')
        """
        op_id = float_op_to_id[op]
        if op_id in (FloatOp.DIV, FloatOp.MOD):
            if not (isinstance(rhs, Float) and rhs.value != 0.0):
                c = self.compare_floats(rhs, Float(0.0), FloatComparisonOp.EQ, line)
                err, ok = BasicBlock(), BasicBlock()
                self.add(Branch(c, err, ok, Branch.BOOL, rare=True))
                self.activate_block(err)
                if op_id == FloatOp.DIV:
                    msg = "float division by zero"
                else:
                    msg = "float modulo"
                self.add(RaiseStandardError(RaiseStandardError.ZERO_DIVISION_ERROR, msg, line))
                self.add(Unreachable())
                self.activate_block(ok)
        if op_id == FloatOp.MOD:
            # Adjust the result to match Python semantics (FloatOp follows C semantics).
            return self.float_mod(lhs, rhs, line)
        else:
            return self.add(FloatOp(lhs, rhs, op_id, line))

    def float_mod(self, lhs: Value, rhs: Value, line: int) -> Value:
        """Perform x % y on floats using Python semantics."""
        mod = self.add(FloatOp(lhs, rhs, FloatOp.MOD, line))
        res = Register(float_rprimitive)
        self.add(Assign(res, mod))
        tricky, adjust, copysign, done = BasicBlock(), BasicBlock(), BasicBlock(), BasicBlock()
        is_zero = self.add(FloatComparisonOp(res, Float(0.0), FloatComparisonOp.EQ, line))
        self.add(Branch(is_zero, copysign, tricky, Branch.BOOL))
        self.activate_block(tricky)
        same_signs = self.is_same_float_signs(lhs, rhs, line)
        self.add(Branch(same_signs, done, adjust, Branch.BOOL))
        self.activate_block(adjust)
        adj = self.float_op(res, rhs, "+", line)
        self.add(Assign(res, adj))
        self.add(Goto(done))
        self.activate_block(copysign)
        # If the remainder is zero, CPython ensures the result has the
        # same sign as the denominator.
        adj = self.primitive_op(copysign_op, [Float(0.0), rhs], line)
        self.add(Assign(res, adj))
        self.add(Goto(done))
        self.activate_block(done)
        return res

    def compare_floats(self, lhs: Value, rhs: Value, op: int, line: int) -> Value:
        return self.add(FloatComparisonOp(lhs, rhs, op, line))

    def int_add(self, lhs: Value, rhs: Value | int) -> Value:
        """Helper to add two native integers.

        The result has the type of lhs.
        """
        if isinstance(rhs, int):
            rhs = Integer(rhs, lhs.type)
        return self.int_op(lhs.type, lhs, rhs, IntOp.ADD, line=-1)

    def int_sub(self, lhs: Value, rhs: Value | int) -> Value:
        """Helper to subtract a native integer from another one.

        The result has the type of lhs.
        """
        if isinstance(rhs, int):
            rhs = Integer(rhs, lhs.type)
        return self.int_op(lhs.type, lhs, rhs, IntOp.SUB, line=-1)

    def int_mul(self, lhs: Value, rhs: Value | int) -> Value:
        """Helper to multiply two native integers.

        The result has the type of lhs.
        """
        if isinstance(rhs, int):
            rhs = Integer(rhs, lhs.type)
        return self.int_op(lhs.type, lhs, rhs, IntOp.MUL, line=-1)

    def fixed_width_int_op(
        self, type: RPrimitive, lhs: Value, rhs: Value, op: int, line: int
    ) -> Value:
        """Generate a binary op using Python fixed-width integer semantics.

        These may differ in overflow/rounding behavior from native/C ops.

        Args:
            type: Either int64_rprimitive or int32_rprimitive
            op: IntOp.* constant (e.g. IntOp.ADD)
        """
        lhs = self.coerce(lhs, type, line)
        rhs = self.coerce(rhs, type, line)
        if op == IntOp.DIV:
            if isinstance(rhs, Integer) and rhs.value not in (-1, 0):
                if not type.is_signed:
                    return self.int_op(type, lhs, rhs, IntOp.DIV, line)
                else:
                    # Inline simple division by a constant, so that C
                    # compilers can optimize more
                    return self.inline_fixed_width_divide(type, lhs, rhs, line)
            if is_int64_rprimitive(type):
                prim = int64_divide_op
            elif is_int32_rprimitive(type):
                prim = int32_divide_op
            elif is_int16_rprimitive(type):
                prim = int16_divide_op
            elif is_uint8_rprimitive(type):
                self.check_for_zero_division(rhs, type, line)
                return self.int_op(type, lhs, rhs, op, line)
            else:
                assert False, type
            return self.call_c(prim, [lhs, rhs], line)
        if op == IntOp.MOD:
            if isinstance(rhs, Integer) and rhs.value not in (-1, 0):
                if not type.is_signed:
                    return self.int_op(type, lhs, rhs, IntOp.MOD, line)
                else:
                    # Inline simple % by a constant, so that C
                    # compilers can optimize more
                    return self.inline_fixed_width_mod(type, lhs, rhs, line)
            if is_int64_rprimitive(type):
                prim = int64_mod_op
            elif is_int32_rprimitive(type):
                prim = int32_mod_op
            elif is_int16_rprimitive(type):
                prim = int16_mod_op
            elif is_uint8_rprimitive(type):
                self.check_for_zero_division(rhs, type, line)
                return self.int_op(type, lhs, rhs, op, line)
            else:
                assert False, type
            return self.call_c(prim, [lhs, rhs], line)
        return self.int_op(type, lhs, rhs, op, line)

    def check_for_zero_division(self, rhs: Value, type: RType, line: int) -> None:
        err, ok = BasicBlock(), BasicBlock()
        is_zero = self.binary_op(rhs, Integer(0, type), "==", line)
        self.add(Branch(is_zero, err, ok, Branch.BOOL))
        self.activate_block(err)
        self.add(
            RaiseStandardError(
                RaiseStandardError.ZERO_DIVISION_ERROR, "integer division or modulo by zero", line
            )
        )
        self.add(Unreachable())
        self.activate_block(ok)

    def inline_fixed_width_divide(self, type: RType, lhs: Value, rhs: Value, line: int) -> Value:
        # Perform floor division (native division truncates)
        res = Register(type)
        div = self.int_op(type, lhs, rhs, IntOp.DIV, line)
        self.add(Assign(res, div))
        same_signs = self.is_same_native_int_signs(type, lhs, rhs, line)
        tricky, adjust, done = BasicBlock(), BasicBlock(), BasicBlock()
        self.add(Branch(same_signs, done, tricky, Branch.BOOL))
        self.activate_block(tricky)
        mul = self.int_op(type, res, rhs, IntOp.MUL, line)
        mul_eq = self.add(ComparisonOp(mul, lhs, ComparisonOp.EQ, line))
        self.add(Branch(mul_eq, done, adjust, Branch.BOOL))
        self.activate_block(adjust)
        adj = self.int_op(type, res, Integer(1, type), IntOp.SUB, line)
        self.add(Assign(res, adj))
        self.add(Goto(done))
        self.activate_block(done)
        return res

    def inline_fixed_width_mod(self, type: RType, lhs: Value, rhs: Value, line: int) -> Value:
        # Perform floor modulus
        res = Register(type)
        mod = self.int_op(type, lhs, rhs, IntOp.MOD, line)
        self.add(Assign(res, mod))
        same_signs = self.is_same_native_int_signs(type, lhs, rhs, line)
        tricky, adjust, done = BasicBlock(), BasicBlock(), BasicBlock()
        self.add(Branch(same_signs, done, tricky, Branch.BOOL))
        self.activate_block(tricky)
        is_zero = self.add(ComparisonOp(res, Integer(0, type), ComparisonOp.EQ, line))
        self.add(Branch(is_zero, done, adjust, Branch.BOOL))
        self.activate_block(adjust)
        adj = self.int_op(type, res, rhs, IntOp.ADD, line)
        self.add(Assign(res, adj))
        self.add(Goto(done))
        self.activate_block(done)
        return res

    def is_same_native_int_signs(self, type: RType, a: Value, b: Value, line: int) -> Value:
        neg1 = self.add(ComparisonOp(a, Integer(0, type), ComparisonOp.SLT, line))
        neg2 = self.add(ComparisonOp(b, Integer(0, type), ComparisonOp.SLT, line))
        return self.add(ComparisonOp(neg1, neg2, ComparisonOp.EQ, line))

    def is_same_float_signs(self, a: Value, b: Value, line: int) -> Value:
        neg1 = self.add(FloatComparisonOp(a, Float(0.0), FloatComparisonOp.LT, line))
        neg2 = self.add(FloatComparisonOp(b, Float(0.0), FloatComparisonOp.LT, line))
        return self.add(ComparisonOp(neg1, neg2, ComparisonOp.EQ, line))

    def comparison_op(self, lhs: Value, rhs: Value, op: int, line: int) -> Value:
        return self.add(ComparisonOp(lhs, rhs, op, line))

    def builtin_len(self, val: Value, line: int, use_pyssize_t: bool = False) -> Value:
        """Generate len(val).

        Return short_int_rprimitive by default.
        Return c_pyssize_t if use_pyssize_t is true (unshifted).
        """
        typ = val.type
        size_value = None
        if is_list_rprimitive(typ) or is_tuple_rprimitive(typ) or is_bytes_rprimitive(typ):
            size_value = self.primitive_op(var_object_size, [val], line)
        elif is_set_rprimitive(typ) or is_frozenset_rprimitive(typ):
            elem_address = self.add(GetElementPtr(val, PySetObject, "used"))
            size_value = self.load_mem(elem_address, c_pyssize_t_rprimitive)
            self.add(KeepAlive([val]))
        elif is_dict_rprimitive(typ):
            size_value = self.call_c(dict_ssize_t_size_op, [val], line)
        elif is_str_rprimitive(typ):
            size_value = self.call_c(str_ssize_t_size_op, [val], line)

        if size_value is not None:
            if use_pyssize_t:
                return size_value
            offset = Integer(1, c_pyssize_t_rprimitive, line)
            return self.int_op(short_int_rprimitive, size_value, offset, IntOp.LEFT_SHIFT, line)

        if isinstance(typ, RInstance):
            # TODO: Support use_pyssize_t
            assert not use_pyssize_t
            length = self.gen_method_call(val, "__len__", [], int_rprimitive, line)
            length = self.coerce(length, int_rprimitive, line)
            ok, fail = BasicBlock(), BasicBlock()
            cond = self.binary_op(length, Integer(0), ">=", line)
            self.add_bool_branch(cond, ok, fail)
            self.activate_block(fail)
            self.add(
                RaiseStandardError(
                    RaiseStandardError.VALUE_ERROR, "__len__() should return >= 0", line
                )
            )
            self.add(Unreachable())
            self.activate_block(ok)
            return length

        # generic case
        if use_pyssize_t:
            return self.call_c(generic_ssize_t_len_op, [val], line)
        else:
            return self.call_c(generic_len_op, [val], line)

    def new_tuple(self, items: list[Value], line: int) -> Value:
        size: Value = Integer(len(items), c_pyssize_t_rprimitive)
        return self.call_c(new_tuple_op, [size] + items, line)

    def new_tuple_with_length(self, length: Value, line: int) -> Value:
        """This function returns an uninitialized tuple.

        If the length is non-zero, the caller must initialize the tuple, before
        it can be made visible to user code -- otherwise the tuple object is broken.
        You might need further initialization with `new_tuple_set_item_op` op.

        Args:
            length: desired length of the new tuple. The rtype should be
                    c_pyssize_t_rprimitive
            line: line number
        """
        return self.call_c(new_tuple_with_length_op, [length], line)

    def int_to_float(self, n: Value, line: int) -> Value:
        return self.primitive_op(int_to_float_op, [n], line)

    def set_immortal_if_free_threaded(self, v: Value, line: int) -> None:
        """Make an object immortal on free-threaded builds (to avoid contention)."""
        if IS_FREE_THREADED and sys.version_info >= (3, 14):
            self.primitive_op(set_immortal_op, [v], line)

    # Internal helpers

    def decompose_union_helper(
        self,
        obj: Value,
        rtype: RUnion,
        result_type: RType,
        process_item: Callable[[Value], Value],
        line: int,
    ) -> Value:
        """Generate isinstance() + specialized operations for union items.

        Say, for Union[A, B] generate ops resembling this (pseudocode):

            if isinstance(obj, A):
                result = <result of process_item(cast(A, obj)>
            else:
                result = <result of process_item(cast(B, obj)>

        Args:
            obj: value with a union type
            rtype: the union type
            result_type: result of the operation
            process_item: callback to generate op for a single union item (arg is coerced
                to union item type)
            line: line number
        """
        # TODO: Optimize cases where a single operation can handle multiple union items
        #     (say a method is implemented in a common base class)
        fast_items = []
        rest_items = []
        for item in rtype.items:
            if isinstance(item, RInstance):
                fast_items.append(item)
            else:
                # For everything but RInstance we fall back to C API
                rest_items.append(item)
        exit_block = BasicBlock()
        result = Register(result_type)
        for i, item in enumerate(fast_items):
            more_types = i < len(fast_items) - 1 or rest_items
            if more_types:
                # We are not at the final item so we need one more branch
                op = self.isinstance_native(obj, item.class_ir, line)
                true_block, false_block = BasicBlock(), BasicBlock()
                self.add_bool_branch(op, true_block, false_block)
                self.activate_block(true_block)
            coerced = self.coerce(obj, item, line)
            temp = process_item(coerced)
            temp2 = self.coerce(temp, result_type, line)
            self.add(Assign(result, temp2))
            self.goto(exit_block)
            if more_types:
                self.activate_block(false_block)
        if rest_items:
            # For everything else we use generic operation. Use force=True to drop the
            # union type.
            coerced = self.coerce(obj, object_rprimitive, line, force=True)
            temp = process_item(coerced)
            temp2 = self.coerce(temp, result_type, line)
            self.add(Assign(result, temp2))
            self.goto(exit_block)
        self.activate_block(exit_block)
        return result

    def translate_special_method_call(
        self,
        base_reg: Value,
        name: str,
        args: list[Value],
        result_type: RType | None,
        line: int,
        can_borrow: bool = False,
    ) -> Value | None:
        """Translate a method call which is handled nongenerically.

        These are special in the sense that we have code generated specifically for them.
        They tend to be method calls which have equivalents in C that are more direct
        than calling with the PyObject api.

        Return None if no translation found; otherwise return the target register.
        """
        primitive_ops_candidates = method_call_ops.get(name, [])
        primitive_op = self.matching_primitive_op(
            primitive_ops_candidates, [base_reg] + args, line, result_type, can_borrow=can_borrow
        )
        return primitive_op

    def translate_eq_cmp(self, lreg: Value, rreg: Value, expr_op: str, line: int) -> Value | None:
        """Add a equality comparison operation.

        Args:
            expr_op: either '==' or '!='
        """
        ltype = lreg.type
        rtype = rreg.type
        if not (isinstance(ltype, RInstance) and ltype == rtype):
            return None

        class_ir = ltype.class_ir
        # Check whether any subclasses of the operand redefines __eq__
        # or it might be redefined in a Python parent class or by
        # dataclasses
        cmp_varies_at_runtime = (
            not class_ir.is_method_final("__eq__")
            or not class_ir.is_method_final("__ne__")
            or class_ir.inherits_python
            or class_ir.is_augmented
        )

        if cmp_varies_at_runtime:
            # We might need to call left.__eq__(right) or right.__eq__(left)
            # depending on which is the more specific type.
            return None

        if not class_ir.has_method("__eq__"):
            # There's no __eq__ defined, so just use object identity.
            identity_ref_op = "is" if expr_op == "==" else "is not"
            return self.translate_is_op(lreg, rreg, identity_ref_op, line)

        return self.gen_method_call(lreg, op_methods[expr_op], [rreg], ltype, line)

    def translate_is_op(self, lreg: Value, rreg: Value, expr_op: str, line: int) -> Value:
        """Create equality comparison operation between object identities

        Args:
            expr_op: either 'is' or 'is not'
        """
        op = ComparisonOp.EQ if expr_op == "is" else ComparisonOp.NEQ
        lhs = self.coerce(lreg, object_rprimitive, line)
        rhs = self.coerce(rreg, object_rprimitive, line)
        return self.add(ComparisonOp(lhs, rhs, op, line))

    def _create_dict(self, keys: list[Value], values: list[Value], line: int) -> Value:
        """Create a dictionary(possibly empty) using keys and values"""
        # keys and values should have the same number of items
        size = len(keys)
        if size > 0:
            size_value: Value = Integer(size, c_pyssize_t_rprimitive)
            # merge keys and values
            items = [i for t in list(zip(keys, values)) for i in t]
            return self.call_c(dict_build_op, [size_value] + items, line)
        else:
            return self.call_c(dict_new_op, [], line)

    def error(self, msg: str, line: int) -> None:
        assert self.errors is not None, "cannot generate errors in this compiler phase"
        self.errors.error(msg, self.module_path, line)


def num_positional_args(arg_values: list[Value], arg_kinds: list[ArgKind] | None) -> int:
    if arg_kinds is None:
        return len(arg_values)
    num_pos = 0
    for kind in arg_kinds:
        if kind == ARG_POS:
            num_pos += 1
    return num_pos
