import re

from parso.python import tree

from jedi._compatibility import zip_longest
from jedi import debug
from jedi.inference.utils import PushBackIterator
from jedi.inference import analysis
from jedi.inference.lazy_value import LazyKnownValue, LazyKnownValues, \
    LazyTreeValue, get_merged_lazy_value
from jedi.inference.names import ParamName, TreeNameDefinition
from jedi.inference.base_value import NO_VALUES, ValueSet, ContextualizedNode
from jedi.inference.value import iterable
from jedi.inference.cache import inference_state_as_method_param_cache
from jedi.inference.param import get_executed_params_and_issues


def try_iter_content(types, depth=0):
    """Helper method for static analysis."""
    if depth > 10:
        # It's possible that a loop has references on itself (especially with
        # CompiledObject). Therefore don't loop infinitely.
        return

    for typ in types:
        try:
            f = typ.py__iter__
        except AttributeError:
            pass
        else:
            for lazy_value in f():
                try_iter_content(lazy_value.infer(), depth + 1)


class ParamIssue(Exception):
    pass


def repack_with_argument_clinic(string, keep_arguments_param=False, keep_callback_param=False):
    """
    Transforms a function or method with arguments to the signature that is
    given as an argument clinic notation.

    Argument clinic is part of CPython and used for all the functions that are
    implemented in C (Python 3.7):

        str.split.__text_signature__
        # Results in: '($self, /, sep=None, maxsplit=-1)'
    """
    clinic_args = list(_parse_argument_clinic(string))

    def decorator(func):
        def wrapper(context, *args, **kwargs):
            if keep_arguments_param:
                arguments = kwargs['arguments']
            else:
                arguments = kwargs.pop('arguments')
            if not keep_arguments_param:
                kwargs.pop('callback', None)
            try:
                args += tuple(_iterate_argument_clinic(
                    context.inference_state,
                    arguments,
                    clinic_args
                ))
            except ParamIssue:
                return NO_VALUES
            else:
                return func(context, *args, **kwargs)

        return wrapper
    return decorator


def _iterate_argument_clinic(inference_state, arguments, parameters):
    """Uses a list with argument clinic information (see PEP 436)."""
    iterator = PushBackIterator(arguments.unpack())
    for i, (name, optional, allow_kwargs, stars) in enumerate(parameters):
        if stars == 1:
            lazy_values = []
            for key, argument in iterator:
                if key is not None:
                    iterator.push_back((key, argument))
                    break

                lazy_values.append(argument)
            yield ValueSet([iterable.FakeSequence(inference_state, u'tuple', lazy_values)])
            lazy_values
            continue
        elif stars == 2:
            raise NotImplementedError()
        key, argument = next(iterator, (None, None))
        if key is not None:
            debug.warning('Keyword arguments in argument clinic are currently not supported.')
            raise ParamIssue
        if argument is None and not optional:
            debug.warning('TypeError: %s expected at least %s arguments, got %s',
                          name, len(parameters), i)
            raise ParamIssue

        value_set = NO_VALUES if argument is None else argument.infer()

        if not value_set and not optional:
            # For the stdlib we always want values. If we don't get them,
            # that's ok, maybe something is too hard to resolve, however,
            # we will not proceed with the type inference of that function.
            debug.warning('argument_clinic "%s" not resolvable.', name)
            raise ParamIssue
        yield value_set


def _parse_argument_clinic(string):
    allow_kwargs = False
    optional = False
    while string:
        # Optional arguments have to begin with a bracket. And should always be
        # at the end of the arguments. This is therefore not a proper argument
        # clinic implementation. `range()` for exmple allows an optional start
        # value at the beginning.
        match = re.match(r'(?:(?:(\[),? ?|, ?|)(\**\w+)|, ?/)\]*', string)
        string = string[len(match.group(0)):]
        if not match.group(2):  # A slash -> allow named arguments
            allow_kwargs = True
            continue
        optional = optional or bool(match.group(1))
        word = match.group(2)
        stars = word.count('*')
        word = word[stars:]
        yield (word, optional, allow_kwargs, stars)
        if stars:
            allow_kwargs = True


class _AbstractArgumentsMixin(object):
    def infer_all(self, funcdef=None):
        """
        Inferes all arguments as a support for static analysis
        (normally Jedi).
        """
        for key, lazy_value in self.unpack():
            types = lazy_value.infer()
            try_iter_content(types)

    def unpack(self, funcdef=None):
        raise NotImplementedError

    def get_executed_params_and_issues(self, execution_context):
        return get_executed_params_and_issues(execution_context, self)

    def get_calling_nodes(self):
        return []


class AbstractArguments(_AbstractArgumentsMixin):
    context = None
    argument_node = None
    trailer = None


class AnonymousArguments(AbstractArguments):
    def get_executed_params_and_issues(self, execution_context):
        from jedi.inference.dynamic import search_params
        return search_params(
            execution_context.inference_state,
            execution_context,
            execution_context.tree_node
        ), []

    def __repr__(self):
        return '%s()' % self.__class__.__name__


def unpack_arglist(arglist):
    if arglist is None:
        return

    # Allow testlist here as well for Python2's class inheritance
    # definitions.
    if not (arglist.type in ('arglist', 'testlist') or (
            # in python 3.5 **arg is an argument, not arglist
            (arglist.type == 'argument') and
            arglist.children[0] in ('*', '**'))):
        yield 0, arglist
        return

    iterator = iter(arglist.children)
    for child in iterator:
        if child == ',':
            continue
        elif child in ('*', '**'):
            yield len(child.value), next(iterator)
        elif child.type == 'argument' and \
                child.children[0] in ('*', '**'):
            assert len(child.children) == 2
            yield len(child.children[0].value), child.children[1]
        else:
            yield 0, child


class TreeArguments(AbstractArguments):
    def __init__(self, inference_state, context, argument_node, trailer=None):
        """
        :param argument_node: May be an argument_node or a list of nodes.
        """
        self.argument_node = argument_node
        self.context = context
        self._inference_state = inference_state
        self.trailer = trailer  # Can be None, e.g. in a class definition.

    @classmethod
    @inference_state_as_method_param_cache()
    def create_cached(cls, *args, **kwargs):
        return cls(*args, **kwargs)

    def unpack(self, funcdef=None):
        named_args = []
        for star_count, el in unpack_arglist(self.argument_node):
            if star_count == 1:
                arrays = self.context.infer_node(el)
                iterators = [_iterate_star_args(self.context, a, el, funcdef)
                             for a in arrays]
                for values in list(zip_longest(*iterators)):
                    # TODO zip_longest yields None, that means this would raise
                    # an exception?
                    yield None, get_merged_lazy_value(
                        [v for v in values if v is not None]
                    )
            elif star_count == 2:
                arrays = self.context.infer_node(el)
                for dct in arrays:
                    for key, values in _star_star_dict(self.context, dct, el, funcdef):
                        yield key, values
            else:
                if el.type == 'argument':
                    c = el.children
                    if len(c) == 3:  # Keyword argument.
                        named_args.append((c[0].value, LazyTreeValue(self.context, c[2]),))
                    else:  # Generator comprehension.
                        # Include the brackets with the parent.
                        sync_comp_for = el.children[1]
                        if sync_comp_for.type == 'comp_for':
                            sync_comp_for = sync_comp_for.children[1]
                        comp = iterable.GeneratorComprehension(
                            self._inference_state,
                            defining_context=self.context,
                            sync_comp_for_node=sync_comp_for,
                            entry_node=el.children[0],
                        )
                        yield None, LazyKnownValue(comp)
                else:
                    yield None, LazyTreeValue(self.context, el)

        # Reordering arguments is necessary, because star args sometimes appear
        # after named argument, but in the actual order it's prepended.
        for named_arg in named_args:
            yield named_arg

    def _as_tree_tuple_objects(self):
        for star_count, argument in unpack_arglist(self.argument_node):
            default = None
            if argument.type == 'argument':
                if len(argument.children) == 3:  # Keyword argument.
                    argument, default = argument.children[::2]
            yield argument, default, star_count

    def iter_calling_names_with_star(self):
        for name, default, star_count in self._as_tree_tuple_objects():
            # TODO this function is a bit strange. probably refactor?
            if not star_count or not isinstance(name, tree.Name):
                continue

            yield TreeNameDefinition(self.context, name)

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.argument_node)

    def get_calling_nodes(self):
        from jedi.inference.dynamic import DynamicExecutedParams
        old_arguments_list = []
        arguments = self

        while arguments not in old_arguments_list:
            if not isinstance(arguments, TreeArguments):
                break

            old_arguments_list.append(arguments)
            for calling_name in reversed(list(arguments.iter_calling_names_with_star())):
                names = calling_name.goto()
                if len(names) != 1:
                    break
                if not isinstance(names[0], ParamName):
                    break
                param = names[0].get_param()
                if isinstance(param, DynamicExecutedParams):
                    # For dynamic searches we don't even want to see errors.
                    return []
                if param.var_args is None:
                    break
                arguments = param.var_args
                break

        if arguments.argument_node is not None:
            return [ContextualizedNode(arguments.context, arguments.argument_node)]
        if arguments.trailer is not None:
            return [ContextualizedNode(arguments.context, arguments.trailer)]
        return []


class ValuesArguments(AbstractArguments):
    def __init__(self, values_list):
        self._values_list = values_list

    def unpack(self, funcdef=None):
        for values in self._values_list:
            yield None, LazyKnownValues(values)

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._values_list)


class TreeArgumentsWrapper(_AbstractArgumentsMixin):
    def __init__(self, arguments):
        self._wrapped_arguments = arguments

    @property
    def context(self):
        return self._wrapped_arguments.context

    @property
    def argument_node(self):
        return self._wrapped_arguments.argument_node

    @property
    def trailer(self):
        return self._wrapped_arguments.trailer

    def unpack(self, func=None):
        raise NotImplementedError

    def get_calling_nodes(self):
        return self._wrapped_arguments.get_calling_nodes()

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._wrapped_arguments)


def _iterate_star_args(context, array, input_node, funcdef=None):
    if not array.py__getattribute__('__iter__'):
        if funcdef is not None:
            # TODO this funcdef should not be needed.
            m = "TypeError: %s() argument after * must be a sequence, not %s" \
                % (funcdef.name.value, array)
            analysis.add(context, 'type-error-star', input_node, message=m)
    try:
        iter_ = array.py__iter__
    except AttributeError:
        pass
    else:
        for lazy_value in iter_():
            yield lazy_value


def _star_star_dict(context, array, input_node, funcdef):
    from jedi.inference.value.instance import CompiledInstance
    if isinstance(array, CompiledInstance) and array.name.string_name == 'dict':
        # For now ignore this case. In the future add proper iterators and just
        # make one call without crazy isinstance checks.
        return {}
    elif isinstance(array, iterable.Sequence) and array.array_type == 'dict':
        return array.exact_key_items()
    else:
        if funcdef is not None:
            m = "TypeError: %s argument after ** must be a mapping, not %s" \
                % (funcdef.name.value, array)
            analysis.add(context, 'type-error-star-star', input_node, message=m)
        return {}
