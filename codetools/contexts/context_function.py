""" Allows a function to execute as if locals are a context
"""
import dis, struct, new
from functools import wraps

##############################################################################
# Implementation Notes
#    This works by shifting all local variable names into the global variable
# names list and then replacing all STORE_*, LOAD_*, and DELETE_* operations
# with the *_NAME versions.
#
#    We might be able to refine somewhat by keeping non-arg locals as *_FAST
#
#    An alternative approach would be to add bytecodes which store the
# arguments from a const list, but although this might end up being faster,
# it requires more complex manipulation of the bytecode (adding instructions
# may mess with jump locations) as well as creating a new code object on every
# function call so that the correct const values can be loaded in.
#
##############################################################################

def parse_bytecode(bytes):
    """ Take a bytecode string and generate (operation, argument) tuples.
    """
    i = 0
    while i < len(bytes):
        op = ord(bytes[i])
        i += 1
        if op >= dis.HAVE_ARGUMENT:
            arg = struct.unpack("<h", bytes[i:i+2])[0]
            i += 2
        else:
            arg = None
        yield dis.opname[op], arg

def compile_bytecode(ops):
    """ Take (operation, argument) tuples and return a bytecode string.
    """
    return ''.join(chr(dis.opmap[op])+(struct.pack('<h', arg)
                                        if arg != None else '')
                    for op, arg in ops)

def patch_load_and_store(ops, argcount, nglobals, ncellandfreevars):
    """ Generator which replaces \*_FAST and \*_GLOBAL ops with \*_NAME ops.
    """
    for op, arg in ops:
        if op  == 'LOAD_FAST':
            op = 'LOAD_NAME'
            arg += nglobals + ncellandfreevars
        elif op == 'STORE_FAST':
            op = 'STORE_NAME'
            arg += nglobals + ncellandfreevars
        elif op == 'DELETE_FAST':
            op = 'DELETE_NAME'
            arg += nglobals + ncellandfreevars
        elif op  == 'LOAD_GLOBAL':
            op = 'LOAD_NAME'
        elif op == 'STORE_GLOBAL':
            op = 'STORE_NAME'
        elif op == 'DELETE_GLOBAL':
            op = 'DELETE_NAME'
        elif op  == 'LOAD_DEREF':
            op = 'LOAD_NAME'
            arg += nglobals
        elif op == 'STORE_DEREF':
            op = 'STORE_NAME'
            arg += nglobals
        elif op == "LOAD_CLOSURE":
            raise ContextFunctionError("can't create context_function for function containing closure")
        elif op in dis.hasname:
            arg += argcount
        yield op, arg

def args_to_locals(co):
    """ Turn arguments of a function into local variables in a code object
    """
    nglobals = len(co.co_names)
    nfreevars = len(co.co_freevars)
    ncellvars = len(co.co_cellvars)
    co_code = compile_bytecode(patch_load_and_store(parse_bytecode(co.co_code),
                                   co.co_argcount, nglobals,
                                   nfreevars+ncellvars))
    return new.code(0, co.co_nlocals+len(co.co_varnames)+nfreevars+ncellvars,
        co.co_stacksize, co.co_flags & ~15,
        co_code, co.co_consts, co.co_names + co.co_cellvars + co.co_freevars
        + co.co_varnames, (),
        co.co_filename, co.co_name, co.co_firstlineno, co.co_lnotab)

def context_function(f, context_factory):
    """ Allows a function to execute as if locals are a context

    This decorator modifies a function so that it uses contexts generated by
    a context_factory in place of the usual local dictionary.  In most cases
    the context_factory function should return a fresh context on each call.

    Potential uses include:
      * over-riding internal globals by pre-inserting values into the local
        namespace (eg. replacing math with numpy in the function's namespace
        so that a function can be converted to use with arrays).
      * internal unit conversion
      * introspection of function operation

    This decorator works by re-writing the function's bytecode, so it will
    not work for functions coming from C extension modules.  It also cannot
    currently work with functions that contain closures.

    Parameters
        f : function
            the function to be decorated
        context_factory : callable
            a callable that returns a context to be used as the function's
            local namespace

    Returns
        a function that can be used in place of f

    Examples
        Over-writing a global in a function using a pre-filled local context

        >>> import math
        >>> def f(x):
        ...     return 2*math.sin(x) + math.cos(x)
        >>> import numpy
        >>> def numpy_math_context():
        ...     return {'math': numpy}
        >>> f = context_function(f, numpy_math_context)
        >>> f(numpy.array([0, 0.5, 1])*numpy.pi)
        array([1.0, 2.1213203435596424, 2])

        Poor-man's closure:

        >>> def accumulator(value):
        ...     total += value
        >>> accumulation_dict = {'total': 0}
        >>> def accumulator_factory():
        ...     return accumulator_dict
        >>> accumulator = context_function(accumulator, accumulator_factory)
        >>> for i in range(10):
        ...     accumulator(i)
        >>> accumulation_dict['total']
        45

        Closure raises an exception:

        >>> def f(x):
        ...     a = 1
        ...     def g(y):
        ...         return y+a
        ...     return x+g(x)
        >>> context_function(f, dict)
        ContextFunctionError: can't create context_function for function containing closure

    """

    # values that we may as well pre-calculate
    code = args_to_locals(f.func_code)
    if f.func_closure:
        free_var_dict = dict(zip(f.func_code.co_freevars[-len(f.func_closure):],
                             (cell.cell_contents for cell in f.func_closure)))
    else:
        free_var_dict = {}

    @wraps(f)
    def new_f(*args, **kwargs):
        loc = context_factory()
        for key, value in free_var_dict.items():
            loc[key] = value
        arg_len = f.func_code.co_argcount
        named_args = f.func_code.co_varnames[:arg_len]
        if f.func_defaults:
            defaults = dict(zip(named_args[-len(f.func_defaults):], f.func_defaults))
        else:
            defaults = {}
        if arg_len < len(args):
            if f.func_code.co_flags & 4:
                loc.update(dict(zip(named_args, args[:arg_len])))
                loc[f.func_code.co_varnames[arg_len]] = args[arg_len:]
                if f.func_code.co_flags & 8:
                    loc[f.func_code.co_varnames[arg_len+1]] = kwargs
            else:
                # too many args
                raise TypeError
        else:
            loc.update(dict(zip(named_args[:len(args)], args)))
            if f.func_code.co_flags & 4:
                loc[f.func_code.co_varnames[arg_len]] = ()
            for arg in named_args[len(args):]:
                if arg in kwargs:
                    loc[arg] = kwargs[arg]
                elif arg in defaults:
                    loc[arg] = defaults[arg]
                else:
                    # not enough args
                    raise TypeError
            for arg in named_args[len(args):]:
                if arg in kwargs:
                    del kwargs[arg]
            if kwargs:
                if f.func_code.co_flags & 8:
                    if f.func_code.co_flags & 4:
                        kwarg_no = arg_len + 1
                    else:
                        kwarg_no = arg_len
                    loc[f.func_code.co_varnames[kwarg_no]] = kwargs
                else:
                    # incorrect kwargs
                    raise TypeError
        return eval(code, f.func_globals, loc)

    return new_f

def local_context(context_factory):
    """ Decorator that specifies a context_factory to be used for this function

    This is a thin wrapper around a context_function call.
    """
    def decorator(f):
        return context_function(f, context_factory)
    return decorator

class ContextFunctionError(ValueError):
    pass


#class ContextFunctionAdapter(HasTraits):
#    implements(IAdapter)
#
#    # the context factory to generate the local namespace of functions
#    context_factory = Function
#
#    def adapt_setitem(self, context, name, value):
#        """
#        """
#        if isinstance(value, types.FunctionType):
#            return context_function(value, self.context_factory)
#        else:
#            return value
#
#class NameContextFunctionAdapter(HasTraits):
#    implements(IAdapter)
#
#    # the context factory to generate the local namespace of functions
#    function_contexts = Dict
#
#    def adapt_setitem(self, context, name, value):
#        """
#        """
#        if isinstance(value, types.FunctionType) and name in self.function_contexts:
#            return context_function(value, self.function_contexts[name])
#        else:
#            return value
