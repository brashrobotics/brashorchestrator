# Wire form of brash.execution-event/v1. The typed fields carry everything the
# monitor and recovery planner need; event_json carries the full payload for
# consumers that want observations and metrics.

builtin_interfaces/Time stamp

string task_id
string node_id
string robot_id
string action

# See brash.execution-event/v1 event_kind.
string event

# See brash.execution-event/v1 error_code.
string error_code

uint16 attempt
string detail
string external_reference

# Full brash.execution-event/v1 document, serialized.
string event_json
