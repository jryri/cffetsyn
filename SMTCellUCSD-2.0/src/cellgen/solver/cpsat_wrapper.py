from ortools.sat.python import cp_model
from ortools.sat.python.cp_model import LinearExpr, LiteralT, IntVar
from loguru import logger

_STR_VAR = {
    cp_model.CHOOSE_FIRST: "CHOOSE_FIRST",
    cp_model.CHOOSE_LOWEST_MIN: "CHOOSE_LOWEST_MIN",
    cp_model.CHOOSE_HIGHEST_MAX: "CHOOSE_HIGHEST_MAX",
    cp_model.CHOOSE_MIN_DOMAIN_SIZE: "CHOOSE_MIN_DOMAIN_SIZE",
    cp_model.CHOOSE_MAX_DOMAIN_SIZE: "CHOOSE_MAX_DOMAIN_SIZE",
}
_STR_DOM = {
    cp_model.SELECT_MIN_VALUE: "SELECT_MIN_VALUE",
    cp_model.SELECT_MAX_VALUE: "SELECT_MAX_VALUE",
    cp_model.SELECT_LOWER_HALF: "SELECT_LOWER_HALF",
    cp_model.SELECT_UPPER_HALF: "SELECT_UPPER_HALF",
}


class LoggingConstraint:
    """Wraps cp_model.Constraint; emits combined log entry on OnlyEnforceIf."""

    def __init__(self, constraint: cp_model.Constraint, model: "CPSAT", ct_str: str):
        self._inner = constraint
        self._model = model
        self._ct_str = ct_str

    def only_enforce_if(self, *lits: LiteralT) -> "LoggingConstraint":
        lit_str = self._model._literal_to_str(lits if len(lits) > 1 else lits[0])
        self._model._log_operation("constraint", f"{self._ct_str} (only_enforce_if) {lit_str}")
        self._inner.only_enforce_if(*lits)
        return self

    OnlyEnforceIf = only_enforce_if

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _logged(op_type: str, fmt):
    """Log call, delegate to super, wrap result if it's a Constraint.

    `fmt` receives (self, *args, **kwargs) and returns the readable constraint string.
    """
    def deco(method):
        def wrapper(self, *args, **kwargs):
            ct_str = fmt(self, *args, **kwargs)
            self._log_operation(op_type, ct_str)
            result = method(self, *args, **kwargs)
            if isinstance(result, cp_model.Constraint):
                return LoggingConstraint(result, self, ct_str)
            return result
        return wrapper
    return deco


class CPSAT(cp_model.CpModel):
    """cp_model.CpModel that logs model-building operations.

    Buffers log messages in memory and flushes in batches. Use as a context
    manager to guarantee a final flush on exit.
    """

    def __init__(self, logfile: str = None, cache_limit: int = 10_000):
        super().__init__()
        self._logfile = logfile
        self._cache_limit = cache_limit
        self._log_cache: list[str] = []
        self._operation_count = 0
        self._comment_count = 0

        if self._logfile:
            with open(self._logfile, "w") as f:
                f.write("CPSAT initialized. Operations will be logged.\n")

        logger.info(f"CPSAT initialized. Logging to '{logfile or 'stdout'}'. Cache limit: {cache_limit}.")

    def _flush_log_cache(self):
        if not self._logfile or not self._log_cache:
            return
        try:
            with open(self._logfile, "a") as f:
                f.write("\n".join(self._log_cache) + "\n")
            self._log_cache.clear()
        except IOError as e:
            logger.error(f"Error flushing log cache: {e}")

    def _log_operation(self, operation_type: str, details: str):
        self._operation_count += 1
        message = f"[CP #{self._operation_count}] Adding {operation_type}:\t{details}"
        if self._logfile:
            self._log_cache.append(message)
            if len(self._log_cache) >= self._cache_limit:
                self._flush_log_cache()

    def log_comment(self, comment: str):
        self._comment_count += 1
        messages = ["", f"[Comment #{self._comment_count}] {comment}"]
        if self._logfile:
            self._log_cache.extend(messages)
            if len(self._log_cache) >= self._cache_limit:
                self._flush_log_cache()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.flush()

    def flush(self):
        """Manually flush buffered logs. Prefer `with` for automatic flushing."""
        if self._logfile:
            logger.info(f"Manual flush requested. Flushing {len(self._log_cache)} logs...")
            self._flush_log_cache()
            logger.info("Flush complete.")

    def _literal_to_str(self, literal_arg) -> str:
        if isinstance(literal_arg, list):
            return str([str(l) for l in literal_arg])
        if hasattr(literal_arg, "Proto"):
            domain = literal_arg.Proto().domain
            if domain == [0, 0]:
                return "False"
            if domain == [1, 1]:
                return "True"
        return str(literal_arg)

    def Proto(self):
        self._log_operation("model_proto", "<raw Proto()>")
        return super().Proto()

    # --- Variables ---
    @_logged("IntVar", lambda self, lb, ub, name: f"name='{name}', domain=[{lb}, {ub}]")
    def NewIntVar(self, lb: int, ub: int, name: str) -> IntVar:
        return super().NewIntVar(lb, ub, name)

    @_logged("BoolVar", lambda self, name: f"name='{name}'")
    def NewBoolVar(self, name: str) -> IntVar:
        return super().NewBoolVar(name)

    @_logged("IntervalVar",
             lambda self, start, size, end, name: f"name='{name}', start={start}, size={size}, end={end}")
    def NewIntervalVar(self, start: LinearExpr, size: LinearExpr, end: LinearExpr, name: str) -> cp_model.IntervalVar:
        return super().NewIntervalVar(start, size, end, name)

    @_logged("IntVarFromDomain", lambda self, domain, name: f"name='{name}', domain={domain}")
    def NewIntVarFromDomain(self, domain: cp_model.Domain, name: str) -> IntVar:
        return super().NewIntVarFromDomain(domain, name)

    @_logged("Constant", lambda self, value: f"value={value}")
    def NewConstant(self, value: int) -> IntVar:
        return super().NewConstant(value)

    # --- Constraints ---
    @_logged("constraint", lambda self, ct: str(ct))
    def Add(self, ct):
        return super().Add(ct)

    @_logged("AllDifferent constraint",
             lambda self, variables: f"AllDifferent({[str(v) for v in variables]})")
    def AddAllDifferent(self, variables: list[IntVar]):
        return super().AddAllDifferent(variables)

    @_logged("Implication constraint",
             lambda self, b1, b2: f"{self._literal_to_str(b1)} => {self._literal_to_str(b2)}")
    def AddImplication(self, b1: LiteralT, b2: LiteralT):
        return super().AddImplication(b1, b2)

    @_logged("AtMostOne constraint",
             lambda self, literals: f"AtMostOne{self._literal_to_str(literals)}")
    def AddAtMostOne(self, literals: list[LiteralT]):
        return super().AddAtMostOne(literals)

    @_logged("ExactlyOne constraint",
             lambda self, literals: f"ExactlyOne{self._literal_to_str(literals)}")
    def AddExactlyOne(self, literals: list[LiteralT]):
        return super().AddExactlyOne(literals)

    @_logged("BoolOr constraint",
             lambda self, literals: f"BoolOr{self._literal_to_str(literals)}")
    def AddBoolOr(self, literals: list[LiteralT]):
        return super().AddBoolOr(literals)

    @_logged("BoolAnd constraint",
             lambda self, literals: f"BoolAnd{self._literal_to_str(literals)}")
    def AddBoolAnd(self, literals: list[LiteralT]):
        return super().AddBoolAnd(literals)

    @_logged("MaxEquality constraint",
             lambda self, mv, exprs: f"{mv} == max({', '.join(str(e) for e in exprs)})")
    def AddMaxEquality(self, max_var: IntVar, exprs: list[LinearExpr]):
        return super().AddMaxEquality(max_var, exprs)

    @_logged("MinEquality constraint",
             lambda self, mv, exprs: f"{mv} == min({', '.join(str(e) for e in exprs)})")
    def AddMinEquality(self, min_var: IntVar, exprs: list[LinearExpr]):
        return super().AddMinEquality(min_var, exprs)

    @_logged("Linear constraint", lambda self, expr, lb, ub: f"{lb} <= {expr} <= {ub}")
    def AddLinearConstraint(self, expr: LinearExpr, lb: int, ub: int):
        return super().AddLinearConstraint(expr, lb, ub)

    @_logged("NoOverlap constraint",
             lambda self, intervals: f"intervals={[str(v) for v in intervals]}")
    def AddNoOverlap(self, interval_vars: list[cp_model.IntervalVar]):
        return super().AddNoOverlap(interval_vars)

    @_logged("Circuit constraint",
             lambda self, arcs: f"arcs=[{', '.join(f'({t},{h},{self._literal_to_str(l)})' for t, h, l in arcs)}]")
    def AddCircuit(self, arcs: list[tuple[int, int, LiteralT]]):
        return super().AddCircuit(arcs)

    @_logged("Cumulative constraint",
             lambda self, intervals, demands, capacity:
                 f"intervals={[str(v) for v in intervals]}, "
                 f"demands={[str(d) for d in demands]}, capacity={capacity}")
    def AddCumulative(self, intervals, demands, capacity):
        return super().AddCumulative(intervals, demands, capacity)

    @_logged("MultiplicationEquality constraint",
             lambda self, target, factors: f"{target} == {' * '.join(str(f) for f in factors)}")
    def AddMultiplicationEquality(self, target: IntVar, factors: list[LinearExpr]):
        return super().AddMultiplicationEquality(target, factors)

    @_logged("DecisionStrategy",
             lambda self, vars_, vs, ds:
                 f"vars={[str(v) for v in vars_]}, "
                 f"var_strategy={_STR_VAR.get(vs, str(vs))}, "
                 f"domain_strategy={_STR_DOM.get(ds, str(ds))}")
    def AddDecisionStrategy(self, variables, var_strategy: int, domain_strategy: int):
        return super().AddDecisionStrategy(variables, var_strategy, domain_strategy)

    # --- Objective ---
    def Minimize(self, expr: LinearExpr):
        self._log_operation("Objective", f"Minimize({expr})")
        super().Minimize(expr)
        self.flush()

    def Maximize(self, expr: LinearExpr):
        self._log_operation("Objective", f"Maximize({expr})")
        super().Maximize(expr)
        self.flush()

    # --- Hints ---
    @_logged("Hint", lambda self, var, value: f"{var} = {value}")
    def AddHint(self, var: IntVar, value: int):
        return super().AddHint(var, value)
