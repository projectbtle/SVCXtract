import os
import sys
import json
from svcxtract.common import paths as common_paths
from svcxtract.core import consts

#============ Generic variables ============
max_time = 300
vendor = None
endian = 'little'
allow_loops = True
max_call_depth = 1
null_value_handling = consts.NULL_HANDLING_NONE

#========== File-specific variables =========

compiler = consts.COMPILER_GCC

# Firmware breakdown.
app_code_base = 0x00000000
disassembly_start_address = 0x00000000
code_start_address = 0x00000000
flash_length = 0x00000000
ram_base = 0x00000000
ram_length = 0x00000000
vector_table_size = 0
application_vector_table = {}
svc_set = {}
firmware_bytes = None
disassembled_firmware = {}
errored_instructions = []
function_blocks = {}
memory_access_functions = {}
blacklisted_functions = []
svc_calls = {}

# Tracing objects.
svc_chains = []
potential_start_points = []