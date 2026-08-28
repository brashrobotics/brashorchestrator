# One measurable robot capability. Limits are typed rather than free text so the
# scheduler can reason about them without vendor-specific code.

string name
bool enabled

# Engineering estimate at first, replaced by a context-dependent success model
# learned from real execution data.
float32 success_prior

float32 max_mass_kg
float32 min_height_m
float32 max_height_m
float32 pose_tolerance_m
float32 yaw_tolerance_deg

# Free-form typed qualifiers, for example terrain types, supported lid kinds, or
# supported object subtypes.
string[] qualifier_keys
string[] qualifier_values
