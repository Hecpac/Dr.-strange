"""Quick smoke test: verify the confidence calibration flow imports and wires up."""
from claw_v2.browse_handler import BrowseHandler
from claw_v2.bot import BotService
from claw_v2.brain import BrainService
from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore
import inspect

# 1. _record_learning_outcome accepts predicted_confidence
sig = inspect.signature(BrowseHandler._record_learning_outcome)
assert "predicted_confidence" in sig.parameters, "missing predicted_confidence param"

# 2. LearningLoop.record accepts predicted_confidence
sig2 = inspect.signature(LearningLoop.record)
assert "predicted_confidence" in sig2.parameters, "missing predicted_confidence in LearningLoop.record"

# 3. MemoryStore has calibration methods
assert hasattr(MemoryStore, "update_calibration_stats"), "missing update_calibration_stats"
assert hasattr(MemoryStore, "get_calibration_stats"), "missing get_calibration_stats"

# 4. BrainService stores _last_confidence
assert "_last_confidence" in BrainService.__dataclass_fields__, "missing _last_confidence field"

print("ALL CHECKS PASSED")
