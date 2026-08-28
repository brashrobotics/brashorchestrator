# An observed change to the world model. A delta is only applied when its source
# is an observation; a backend success claim alone never mutates verified state.

builtin_interfaces/Time stamp

string task_id
string node_id

# OBSERVATION, BACKEND_CLAIM, SCENARIO_LOAD, OPERATOR, RECOVERY.
# Only OBSERVATION marks the resulting facts verified.
string source

# Predicates now known true, for example "on(drill_01, shelf_B12)".
string[] asserted

# Predicates now known false.
string[] retracted

# Attribute updates as "entity_id.attribute=value".
string[] attributes

# 0.0 to 1.0 perception confidence for the whole delta.
float32 confidence
