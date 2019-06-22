# Copyright (C) 2018 Ben North
#
# This file is part of 'plausibility argument of concept for compiling
# Python into Amazon Step Function state machine JSON'.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import ast
import attr
from functools import reduce
from operator import concat
import click
import json


########################################################################

def psf_attr(nd, raise_if_not=True):
    """
    Extract the attribute name from an AST node of the form

        PSF.something

    If the given AST node is not of that form, either raise a
    ValueError (if raise_if_not is True), or return None (if
    raise_if_not is False).
    """
    attr_val = None
    if isinstance(nd, ast.Attribute):
        val = nd.value
        if isinstance(val, ast.Name) and val.id == 'PSF':
            attr_val = nd.attr
    if attr_val is None and raise_if_not:
        raise ValueError('expected PSF.something')
    return attr_val


def chained_key(nd):
    """
    Given an AST node representing a value like

        foo['bar']['baz']

    return a list of the components involved; here,

        ['foo', 'bar', 'baz']

    If the given node is not of that form, raise a ValueError.
    """
    if isinstance(nd, ast.Name):
        return [nd.id]
    if isinstance(nd, ast.Subscript):
        if isinstance(nd.slice, ast.Index):
            if isinstance(nd.slice.value, ast.Str):
                suffix = nd.slice.value.s
                if isinstance(nd.value, ast.Name):
                    prefix = [nd.value.id]
                else:
                    prefix = chained_key(nd.value)
                return prefix + [suffix]
    raise ValueError('expected chained lookup via strings on name')


def chained_key_smr(k):
    """
    Convert a sequence of chained lookups into the jsonPath which will
    refer to its location in the 'locals' object.
    """
    return '.'.join(['$', 'locals'] + k)


def lmap(f, xs):
    return list(map(f, xs))


def maybe_with_next(base_fields, next_state_name):
    """
    Return a copy of base_fields (a dict), with an additional item

        'Next': next_state_name

    iff next_state_name is non-None.
    """
    obj = dict(base_fields)
    if next_state_name is not None:
        obj['Next'] = next_state_name
    return obj


########################################################################

class ChoiceConditionIR:
    @staticmethod
    def from_ast_node(nd):
        if isinstance(nd, ast.Call):
            return TestComparisonIR.from_ast_node(nd)
        elif isinstance(nd, ast.BoolOp):
            return TestCombinatorIR.from_ast_node(nd)
        raise ValueError('expected Call')


@attr.s
class TestComparisonIR(ChoiceConditionIR):
    predicate_name = attr.ib()
    predicate_variable = attr.ib()
    predicate_literal = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        if isinstance(nd, ast.Call) and len(nd.args) == 2:
            return cls(psf_attr(nd.func),
                       chained_key(nd.args[0]),
                       nd.args[1].s)
        raise ValueError('expected function-call PSF.something(...)')

    def as_choice_rule_smr(self, next_state_name):
        return maybe_with_next(
            {'Variable': chained_key_smr(self.predicate_variable),
             self.predicate_name: self.predicate_literal},
            next_state_name)


@attr.s
class TestCombinatorIR(ChoiceConditionIR):
    opname = attr.ib()
    values = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        if isinstance(nd, ast.BoolOp):
            if isinstance(nd.op, ast.Or):
                opname = 'Or'
            elif isinstance(nd.op, ast.And):
                opname = 'And'
            else:
                raise ValueError('expected Or or And')
            return cls(opname, lmap(ChoiceConditionIR.from_ast_node, nd.values))
        raise ValueError('expected BoolOp')

    def as_choice_rule_smr(self, next_state_name):
        terms = [v.as_choice_rule_smr(None) for v in self.values]
        return maybe_with_next(
            {self.opname: terms},
            next_state_name)


########################################################################

@attr.s
class RetrySpecIR:
    error_equals = attr.ib()
    interval_seconds = attr.ib()
    max_attempts = attr.ib()
    backoff_rate = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        return cls([error_name.s for error_name in nd.elts[0].elts],
                   nd.elts[1].n,
                   nd.elts[2].n,
                   nd.elts[3].n)

    def as_json_obj(self):
        return {'ErrorEquals': self.error_equals,
                'IntervalSeconds': self.interval_seconds,
                'MaxAttempts': self.max_attempts,
                'BackoffRate': self.backoff_rate}


@attr.s
class CatcherIR:
    error_equals = attr.ib()
    body = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        return cls([nd.type.id], SuiteIR.from_ast_nodes(nd.body))


########################################################################

@attr.s
class ReturnIR:
    varname = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        if isinstance(nd.value, ast.Name):
            return cls(nd.value.id)
        raise ValueError('expected return of variable')

    def as_fragment(self, xln_ctx):
        s = StateMachineStateIR.from_fields(
            Type='Succeed',
            InputPath=chained_key_smr([self.varname]))
        return StateMachineFragmentIR([s], s, [])


@attr.s
class RaiseIR:
    error = attr.ib()
    cause = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        if (isinstance(nd.exc, ast.Call)
                and psf_attr(nd.exc.func) == 'Fail'
                and len(nd.exc.args) == 2
                and isinstance(nd.exc.args[0], ast.Str)
                and isinstance(nd.exc.args[1], ast.Str)):
            return cls(nd.exc.args[0].s, nd.exc.args[1].s)
        raise ValueError('expected raise PSF.Fail("foo", "bar")')

    def as_fragment(self, xln_ctx):
        s = StateMachineStateIR.from_fields(
            Type='Fail', Error=self.error, Cause=self.cause)
        return StateMachineFragmentIR([s], s, [])


class AssignmentSourceIR:
    @classmethod
    def from_ast_node(cls, nd, defs):
        if isinstance(nd, ast.Call):
            if (isinstance(nd.func, ast.Name)
                    or (isinstance(nd.func, ast.Attribute)
                        and psf_attr(nd.func) == 'with_retry_spec')):
                return FunctionCallIR.from_ast_node(nd)
            if (isinstance(nd.func, ast.Attribute)
                    and psf_attr(nd.func) == 'parallel'):
                return ParallelIR.from_ast_node_and_defs(nd, defs)
        raise ValueError('expected fn(x, y)'
                         ' or PSF.with_retry_spec(fn, (x, y), s1, s2)')


@attr.s
class FunctionCallIR(AssignmentSourceIR):
    fun_name = attr.ib()
    arg_names = attr.ib()
    retry_spec = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        if isinstance(nd, ast.Call):
            if not isinstance(nd.func, ast.Attribute):
                # Bare call
                return cls(nd.func.id, [a.id for a in nd.args], None)
            elif psf_attr(nd.func) == 'with_retry_spec':
                return cls(nd.args[0].id,
                           [a.id for a in nd.args[1].elts],
                           lmap(RetrySpecIR.from_ast_node, nd.args[2:]))
        raise ValueError('expected some_function(some, args)'
                         ' or PSF.with_retry_spec(fun, (some, args),'
                         ' retry_spec_1, retry_spec_2)')

    def call_descriptor(self):
        return {"function": self.fun_name, "arg_names": self.arg_names}

    def as_fragment(self, xln_ctx, target_varname):
        s_pass = StateMachineStateIR.from_fields(Type='Pass',
                                                 Result=self.call_descriptor(),
                                                 ResultPath='$.call_descr')

        task_fields = {'Type': 'Task',
                       'Resource': xln_ctx.lambda_arn,
                       'ResultPath': chained_key_smr([target_varname])}
        if self.retry_spec is not None:
            task_fields['Retry'] = [s.as_json_obj() for s in self.retry_spec]
        s_task = StateMachineStateIR.from_fields(**task_fields)

        s_pass.next_state_name = s_task.name

        return StateMachineFragmentIR([s_pass, s_task], s_pass, [s_task])


@attr.s
class ParallelIR:
    branches = attr.ib()

    @classmethod
    def from_ast_node_and_defs(cls, nd, defs):
        branch_names = [arg.id for arg in nd.args]
        return cls([defs[n] for n in branch_names])

    def as_fragment(self, xln_ctx, target_varname):
        # The branches of a 'Parallel' are isolated state machines, so
        # we need to convert each one into a JSON-friendly form now.
        # This is in contrast to 'If' or 'Try' where the bodies
        # contribute their states to the top-level state machine.
        s_parallel = StateMachineStateIR.from_fields(
            Type='Parallel',
            Branches=[branch.as_fragment(xln_ctx).as_json_obj()
                      for branch in self.branches],
            ResultPath=chained_key_smr([target_varname]))
        return StateMachineFragmentIR([s_parallel], s_parallel, [s_parallel])


class StatementIR:
    @classmethod
    def from_ast_node(self, nd, defs):
        if isinstance(nd, ast.Assign):
            return AssignmentIR.from_ast_node(nd, defs)
        if isinstance(nd, ast.Try):
            return TryIR.from_ast_node(nd)
        if isinstance(nd, ast.If):
            return IfIR.from_ast_node(nd)
        if isinstance(nd, ast.Return):
            return ReturnIR.from_ast_node(nd)
        if isinstance(nd, ast.Raise):
            return RaiseIR.from_ast_node(nd)
        raise ValueError('unexpected node type {} for statement'
                         .format(type(nd)))


@attr.s
class AssignmentIR(StatementIR):
    target_varname = attr.ib()
    source = attr.ib()

    @classmethod
    def from_ast_node(cls, nd, defs):
        if isinstance(nd, ast.Assign) and len(nd.targets) == 1:
            return cls(nd.targets[0].id,
                       AssignmentSourceIR.from_ast_node(nd.value, defs))
        raise ValueError('expected single-target assignment')

    def as_fragment(self, xln_ctx):
        return self.source.as_fragment(xln_ctx, self.target_varname)


@attr.s
class TryIR(StatementIR):
    body = attr.ib()
    catchers = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        assert len(nd.body) == 1
        body = SuiteIR.from_ast_nodes(nd.body)
        return cls(body, [CatcherIR.from_ast_node(h) for h in nd.handlers])

    def as_fragment(self, xln_ctx):
        body = self.body.as_fragment(xln_ctx)
        catcher_fragments = [c.body.as_fragment(xln_ctx) for c in self.catchers]
        s_task = body.all_states[1]
        assert s_task.fields['Type'] == 'Task'
        s_task.fields['Catch'] = [
            {'ErrorEquals': c.error_equals, 'Next': f.enter_state.name}
            for (c, f) in zip(self.catchers, catcher_fragments)]

        all_catcher_states = reduce(
            concat, [f.all_states for f in catcher_fragments], [])

        all_catcher_exits = reduce(
            concat, [f.exit_states for f in catcher_fragments], [])

        return StateMachineFragmentIR(
            body.all_states + all_catcher_states,
            body.enter_state,
            body.exit_states + all_catcher_exits)


@attr.s
class IfIR(StatementIR):
    test = attr.ib()
    true_body = attr.ib()
    false_body = attr.ib()

    @classmethod
    def from_ast_node(cls, nd):
        return cls(ChoiceConditionIR.from_ast_node(nd.test),
                   SuiteIR.from_ast_nodes(nd.body),
                   SuiteIR.from_ast_nodes(nd.orelse))

    def as_fragment(self, xln_ctx):
        true_frag = self.true_body.as_fragment(xln_ctx)
        false_frag = self.false_body.as_fragment(xln_ctx)

        choice_rule = self.test.as_choice_rule_smr(true_frag.enter_state.name)
        choice_state = StateMachineStateIR.from_fields(
            Type='Choice',
            Choices=[choice_rule],
            Default=false_frag.enter_state.name)

        all_states = ([choice_state]
                      + true_frag.all_states
                      + false_frag.all_states)

        exit_states = true_frag.exit_states + false_frag.exit_states

        return StateMachineFragmentIR(all_states, choice_state, exit_states)


@attr.s
class SuiteIR:
    body = attr.ib()

    @classmethod
    def from_ast_nodes(cls, nds):
        body = []
        defs = {}
        for nd in nds:
            if isinstance(nd, ast.FunctionDef):
                defs[nd.name] = SuiteIR.from_ast_nodes(nd.body)
            else:
                body.append(StatementIR.from_ast_node(nd, defs))
        return cls(body)

    def as_fragment(self, xln_ctx):
        fragments = [stmt.as_fragment(xln_ctx) for stmt in self.body]
        for f0, f1 in zip(fragments[:-1], fragments[1:]):
            f0.set_next_state(f1.enter_state.name)
        return StateMachineFragmentIR(
            reduce(concat, [f.all_states for f in fragments], []),
            fragments[0].enter_state,
            fragments[-1].exit_states)


########################################################################

@attr.s
class TranslationContext:
    lambda_arn = attr.ib()

    @staticmethod
    def is_main_fundef(fd):
        return (
            isinstance(fd, ast.FunctionDef)
            and len(fd.decorator_list) == 1
            and psf_attr(fd.decorator_list[0], raise_if_not=False) == 'main')

    def state_machine_main_fundef(self, syntax_tree):
        candidates = [x for x in syntax_tree.body if self.is_main_fundef(x)]
        if len(candidates) != 1:
            raise ValueError('no unique PSF.main function')
        return candidates[0]

    def top_level_state_machine(self, syntax_tree):
        fun = self.state_machine_main_fundef(syntax_tree)
        suite = SuiteIR.from_ast_nodes(fun.body)
        return suite.as_fragment(self)


@attr.s
class StateMachineStateIR:
    name = attr.ib()
    fields = attr.ib()
    next_state_name = attr.ib()

    next_id = 0

    @classmethod
    def from_fields(cls, **kwargs):
        name = 'n{}'.format(cls.next_id)
        cls.next_id += 1
        return cls(name, kwargs, None)

    def value_as_json_obj(self):
        return maybe_with_next(self.fields, self.next_state_name)


@attr.s
class StateMachineFragmentIR:
    all_states = attr.ib()
    enter_state = attr.ib()
    exit_states = attr.ib()

    @property
    def n_states(self):
        return len(self.all_states)

    def set_next_state(self, next_state_name):
        for s in self.exit_states:
            s.next_state_name = next_state_name

    def as_json_obj(self):
        return {'States': {s.name: s.value_as_json_obj()
                           for s in self.all_states},
                'StartAt': self.enter_state.name}


########################################################################

@click.command()
@click.argument('source_fname')
@click.argument('lambda_arn')
def main(source_fname, lambda_arn):
    syntax_tree = ast.parse(source=open(source_fname, 'rt').read(),
                            filename=source_fname)

    xln_ctx = TranslationContext(lambda_arn)
    state_machine = xln_ctx.top_level_state_machine(syntax_tree)
    print(json.dumps(state_machine.as_json_obj(), indent=2))


if __name__ == '__main__':
    main()
