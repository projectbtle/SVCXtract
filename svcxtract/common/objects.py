import os
import sys
import json
from svcxtract.common import paths as common_paths
from svcxtract.core import consts

# Variables.
compiler = consts.COMPILER_GCC
vendor = None

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