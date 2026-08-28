# Normalized robot state. Produced either from a Fleet Control snapshot or from
# mirrored embodied-controller telemetry.

builtin_interfaces/Time stamp

string robot_id
string frame_id

# ONLINE, DEGRADED, OFFLINE
string connection

# AVAILABLE, BUSY, CHARGING, QUARANTINED, FAULTED
string availability

float32 battery_percent

string location_id
float32 x
float32 y
float32 yaw

string active_task_id
string active_node_id

# Non-empty when the robot reports a fault. Uses the shared error-code set.
string fault_code

# Currently carried object identifiers.
string[] payload_object_ids
float32 payload_mass_kg
