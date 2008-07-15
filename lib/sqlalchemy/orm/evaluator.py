from sqlalchemy.sql import operators, functions
from sqlalchemy.sql import expression as sql
from sqlalchemy.util import Set
import operator

class UnevaluatableError(Exception):
    pass

_straight_ops = Set([getattr(operators, op) for op in [
    'add', 'mul', 'sub', 'div', 'mod', 'truediv', 'lt', 'le', 'ne', 'gt', 'ge', 'eq'
]])


_notimplemented_ops = Set([getattr(operators, op) for op in [
    'like_op', 'notlike_op', 'ilike_op', 'notilike_op', 'between_op', 'in_op', 'notin_op',
    'endswith_op', 'concat_op',
]])

class EvaluatorCompiler(object):
    def process(self, clause):
        meth = getattr(self, "visit_%s" % clause.__visit_name__, None)
        if not meth:
            raise UnevaluatableError("Cannot evaluate %s" % type(clause).__name__)
        return meth(clause)

    def visit_grouping(self, clause):
        return self.process(clause.element)

    def visit_null(self, clause):
        return lambda obj: None

    def visit_column(self, clause):
        if 'parententity' in clause._annotations:
            key = clause._annotations['parententity']._get_col_to_prop(clause).key
        else:
            key = clause.key
        get_corresponding_attr = operator.attrgetter(key)
        return lambda obj: get_corresponding_attr(obj)

    def visit_clauselist(self, clause):
        evaluators = map(self.process, clause.clauses)
        if clause.operator is operators.or_:
            def evaluate(obj):
                has_null = False
                for sub_evaluate in evaluators:
                    value = sub_evaluate(obj)
                    if value:
                        return True
                    has_null = has_null or value is None
                if has_null:
                    return None
                return False
        if clause.operator is operators.and_:
            def evaluate(obj):
                for sub_evaluate in evaluators:
                    value = sub_evaluate(obj)
                    if not value:
                        if value is None:
                            return None
                        return False
                return True

        return evaluate

    def visit_binary(self, clause):
        eval_left,eval_right = map(self.process, [clause.left, clause.right])
        operator = clause.operator
        if operator is operators.is_:
            def evaluate(obj):
                return eval_left(obj) == eval_right(obj)
        if operator is operators.isnot:
            def evaluate(obj):
                return eval_left(obj) != eval_right(obj)
        elif operator in _straight_ops:
            def evaluate(obj):
                left_val = eval_left(obj)
                right_val = eval_right(obj)
                if left_val is None or right_val is None:
                    return None
                return operator(eval_left(obj), eval_right(obj))
        else:
            raise UnevaluatableError("Cannot evaluate %s with operator %s" % (type(clause).__name__, clause.operator))
        return evaluate

    def visit_unary(self, clause):
        eval_inner = self.process(clause.element)
        if clause.operator is operators.inv:
            def evaluate(obj):
                value = eval_inner(obj)
                if value is None:
                    return None
                return not value
            return evaluate
        raise UnevaluatableError("Cannot evaluate %s with operator %s" % (type(clause).__name__, clause.operator))

    def visit_bindparam(self, clause):
        val = clause.value
        return lambda obj: val
