import os
import sys
import struct
import logging
import numpy as np

from capstone import *
from capstone.arm import *
from collections import Counter

from argxtract.core import utils
from argxtract.core import consts
from argxtract.core import binary_operations as binops
from argxtract.common import paths as common_paths
from argxtract.common import objects as common_objs
from argxtract.core.strand_execution import StrandExecution

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB + CS_MODE_LITTLE_ENDIAN)
# Turn on SKIPDATA mode - this is needed!
md.skipdata = True
md.detail = True


class FirmwareDisassembler:
    def __init__(self):
        self.arm_switch8 = None
        
    def estimate_app_code_base(self):
        logging.info('Estimating app code base.')
        
        """ This is quite a hacky way of doing things, but with stripped 
            binaries, we have very little to go on. 
            We first get the addresses for interrupt handlers from vector table. 
            We then look for all branch statements with <self> as target address.
            We then compare the last 3 hex values of addresses, and get matches.
            App code base is then 
                (vector_table_entry_address - self_targeting_branch_address)
        """
        
        # Initialise app code base.
        app_code_base = 0x00000000
        
        # DIsassemble the firmware.
        self.create_disassembled_object()
        
        # Populate interrupt handler addresses.
        interrupt_handlers = []
        reset_address = common_objs.application_vector_table['reset']
        for key in common_objs.application_vector_table:
            # Reset Handler is never an endless loop.
            # Also leave out SysTick Handler, because it's optional
            #  in Cortex-M0.
            if key in ['initial_sp', 'reset', 'systick']:
                continue
            address = '{0:08x}'.format(common_objs.application_vector_table[key])
            interrupt_handlers.append(address)
        
        # Estimate default handler.
        default_handler = self.estimate_default_handler()
        if default_handler != None:
            if default_handler not in interrupt_handlers:
                logging.trace(
                    'Default handler estimated to be '
                    + default_handler
                )
                interrupt_handlers.append(default_handler)

        # Populate self-targeting branch addresses.
        self_targeting_branches = self.populate_self_targeting_branches()
        
        if len(self_targeting_branches) == 0:
            logging.debug(
                'No self-targeting branches. App code base cannot be determined.'
            )

        # Check the self-targeting branches against interrupt handlers.
        # Hopefully there isn't more than one match.
        possible_code_bases = self.estimate_code_base(
            interrupt_handlers, 
            self_targeting_branches,
            -3, # Check last 3 hex chars.
            reset_address
        )
                    
        if len(list(set(possible_code_bases))) == 0:
            logging.trace('Trying lower accuracy app code base estimation.')
            possible_code_bases = self.estimate_code_base(
                interrupt_handlers, 
                self_targeting_branches,
                -2, # Check last 2 hex chars.
                reset_address
            )
                
        if len(list(set(possible_code_bases))) == 1:
            app_code_base = possible_code_bases[0]
        elif len(list(set(possible_code_bases))) > 1:
            code_base_str = ''
            for possible_code_base in possible_code_bases:
                code_base_str = code_base_str + hex(possible_code_base) + ';'
            logging.warning(
                'More than one possibility for app code base: '
                + code_base_str
            )
            c = Counter(possible_code_bases)
            possible_app_code_base, count = c.most_common()[0]
            if count > 1:
                app_code_base = possible_app_code_base
            else:    
                # Prioritise the code base that was estimated using default handler.
                if default_handler != None:
                    possible_code_bases = self.estimate_code_base(
                        [default_handler], 
                        self_targeting_branches,
                        -3, # Check last 3 hex chars.
                        reset_address
                    )
                    if len(possible_code_bases) == 0:
                        possible_code_bases = self.estimate_code_base(
                            [default_handler], 
                            self_targeting_branches,
                            -2, # Check last 2 hex chars.
                            reset_address
                        )
                    c = Counter(possible_code_bases)
                    app_code_base, count = c.most_common()[0]                            
            logging.trace('Using ' + hex(app_code_base) + ' as app code base.')

        # If the reset handler doesn't fit into the range:
        #  (app_code_base, app_code_base+file_size)
        #  then estimate code base.
        if ((reset_address < app_code_base) 
                or (reset_address >= (app_code_base + len(common_objs.core_bytes)))):
            logging.debug(
                'App code base does not include reset handler. '
            )
            common_objs.app_code_base = None
            return
            
        common_objs.app_code_base = app_code_base
        common_objs.disassembly_start_address = app_code_base

        # Populate self-targeting branches, with app code base offset)
        for self_targeting_branch in self_targeting_branches:
            common_objs.self_targeting_branches.append(
                int(self_targeting_branch, 16) + 
                common_objs.app_code_base
            )
        logging.info('App code base estimated as: ' + hex(app_code_base))

    def read_vector_table(self, base=0):
        application_vector_table = {}
        image_file = open(common_paths.path_to_fw, 'rb')
        for avt_entry in consts.AVT.keys():
            image_file.seek(0)
            image_file.seek(base+consts.AVT[avt_entry])
            vector_table_entry = struct.unpack('<I', image_file.read(4))[0]
            if avt_entry == 'initial_sp':
                if vector_table_entry == 0x00000000:
                    return False
                if vector_table_entry%2 != 0:
                    return False
            elif avt_entry == 'reset':
                if vector_table_entry == 0x00000000:
                    return False
                if vector_table_entry%2 != 1:
                    return False
            elif avt_entry != 'systick':
                if vector_table_entry == 0x00000000:
                    continue
                if vector_table_entry%2 != 1:
                    return False
            if vector_table_entry%2 == 1:
                vector_table_entry -= 1
            application_vector_table[avt_entry] = vector_table_entry
        
        common_objs.application_vector_table = application_vector_table
        debug_msg = 'Partial Application Vector Table:'
        for avt_entry in application_vector_table:
            debug_msg += '\n\t\t\t\t\t\t\t\t' \
                         + avt_entry \
                         + ': ' \
                         + hex(application_vector_table[avt_entry]) 
        logging.info(debug_msg)
        return True
    
    def estimate_default_handler(self):
        logging.trace('Estimating default handler')
        interrupt_handlers = []
        
        for key in common_objs.application_vector_table:
            if key in ['initial_sp', 'reset', 'systick']:
                continue
            if common_objs.application_vector_table[key] == 0:
                continue
            interrupt_handlers.append(common_objs.application_vector_table[key])
        c = Counter(interrupt_handlers)
        most_common, count = c.most_common()[0]
        if count > 1:
            return '{0:08x}'.format(most_common)
        
        min_value = min(interrupt_handlers)
        max_value = max(interrupt_handlers)
        file_size = len(common_objs.core_bytes) - 0x3c
        
        image_file = open(common_paths.path_to_fw, 'rb')
        address = 0x3c-4
        while address < 0x400:
            address += 4
            image_file.seek(0)
            image_file.seek(address)
            vector_table_entry = struct.unpack('<I', image_file.read(4))[0]
            if vector_table_entry == 0:
                continue
            if vector_table_entry == 0xffffffff:
                continue
            if vector_table_entry%2 == 0:
                break
            if vector_table_entry < min_value:
                if ((max_value-vector_table_entry) > file_size):
                    break
            if vector_table_entry > max_value:
                if ((vector_table_entry-min_value) > file_size):
                    break
            interrupt_handlers.append(vector_table_entry-1)
            
        c = Counter(interrupt_handlers)
        most_common, count = c.most_common()[0]
        if count > 1:
            return '{0:08x}'.format(most_common)
        return None
    
    def estimate_code_base(self, interrupt_handlers, self_targeting_branches,
            num_hex, reset_address):
        possible_code_bases = []
        for interrupt_handler in interrupt_handlers:
            for self_targeting_branch in self_targeting_branches:
                logging.trace(
                    'Testing interrupt handler ' 
                    + interrupt_handler
                    + ' against self targeting branch at '
                    + self_targeting_branch
                )
                current_app_code_base = 0
                if (self_targeting_branch.replace('0x', ''))[num_hex:] == interrupt_handler[num_hex:]:
                    current_app_code_base = \
                        int(interrupt_handler, 16) - int(self_targeting_branch, 16)
                if current_app_code_base < 0: continue
                # The range of values must include the Reset Handler.
                min_range = current_app_code_base
                max_range = current_app_code_base + len(common_objs.core_bytes)
                if reset_address < min_range:
                    continue
                if reset_address > max_range:
                    continue
                logging.trace('Match found!')
                possible_code_bases.append(current_app_code_base)
        return possible_code_bases
    
    def populate_self_targeting_branches(self):
        self_targeting_branches = []
        for ins_address in common_objs.disassembled_firmware:
            if utils.is_valid_code_address(ins_address) != True:
                continue
            insn = common_objs.disassembled_firmware[ins_address]['insn']
            opcode_id = insn.id
            
            # Check whether the opcode is for a branch instruction at all.
            # Basic branches are easy.
            if opcode_id in [ARM_INS_BL, ARM_INS_B]:
                target_address_int = insn.operands[0].value.imm
                target_address = '{0:08x}'.format(target_address_int)
                if target_address_int == ins_address:
                    self_targeting_branches.append(target_address)
            # BX Rx is more complicated.
            # This would be in the form of LDR Rx, [PC, offset], 
            #  followed by BX Rx
            if opcode_id == ARM_INS_BX:
                branch_register = insn.operands[0].value.reg
                # LDR normally doesn't load to LR?
                if branch_register == ARM_REG_LR:
                    continue
                # Firstly, we assume that such functions don't have
                #  a large number of instructions. One or two at most.
                # The LDR is assumed to be the immediately preceding
                #  instruction.
                if (ins_address-2) not in common_objs.disassembled_firmware:
                    continue
                if self.check_valid_pc_ldr(ins_address-2) != True:
                    continue
                prev_insn = common_objs.disassembled_firmware[ins_address-2]['insn']
                if prev_insn == None: 
                    continue
                if prev_insn.id != ARM_INS_LDR:
                    continue
                ldr_address = ins_address-2
                curr_pc_value = self.get_mem_access_pc_value(ldr_address)
                ldr_target = curr_pc_value + prev_insn.operands[1].mem.disp
                data_bytes = self.get_ldr_target_data_bytes(ldr_target, 4)
                if data_bytes == consts.ERROR_INVALID_INSTRUCTION:
                    if ldr_address not in common_objs.errored_instructions:
                        common_objs.errored_instructions.append(ldr_address)
                        logging.trace(
                            'Unable to get data bytes for load instruction at '
                            + hex(ldr_address)
                            + '. Adding to errored instructions.'
                        )
                    continue
                if data_bytes == '':
                    if ldr_address not in common_objs.errored_instructions:
                        common_objs.errored_instructions.append(ldr_address)
                        logging.trace(
                            'Empty data bytes for load instruction at '
                            + hex(ldr_address)
                            + '. Adding to errored instructions.'
                        )
                    continue
                ordered_bytes = int(data_bytes, 16)
                target_branch = ordered_bytes - 1 # Thumb mode needs -1
                formatted_target_branch = '{0:08x}'.format(target_branch)
                formatted_ins_address = '{0:08x}'.format(ins_address)
                if formatted_ins_address[-3:] == formatted_target_branch[-3:]:
                    self_targeting_branches.append(formatted_ins_address)
        self_targeting_branches.sort()       
        return self_targeting_branches
        
    def estimate_vector_table_size(self):
        # At a minimum, the vector table will have 15 entries
        vector_table_size = (4*15)
        file_size_in_bytes = os.stat(common_paths.path_to_fw).st_size
        address_min = vector_table_size
        address_max = file_size_in_bytes
        image_file = open(common_paths.path_to_fw, 'rb')
        
        app_code_base = common_objs.app_code_base
        if app_code_base == None:
            app_code_base = 0
            
        address = vector_table_size
        is_code = False
        while (is_code == False):
            if address >= 1024: break
            
            image_file.seek(0)
            image_file.seek(address)
            entry = struct.unpack('<I', image_file.read(4))[0]
            if ((entry == 0) or (entry == 0xffffffff)):
                address += 4
                continue
            if entry % 2 == 0:
                break
            relative_entry = entry - 1 - app_code_base
            if ((relative_entry >= address_min) 
                    and (relative_entry < address_max)):
                address += 4
                continue
            # Sometimes, VT appears to contain entries to underlying stack.
            if entry < app_code_base:
                address += 4
                continue
            break

        vector_table_size = address   
        common_objs.vector_table_size = vector_table_size
        logging.info(
            'Vector table size computed as ' 
            + hex(vector_table_size)
        )
        common_objs.code_start_address = \
            app_code_base + vector_table_size
        logging.info(
            'Start of code is ' 
            + hex(common_objs.code_start_address)
        )
    
    def create_disassembled_object(self):
        if common_objs.disassembled_firmware == {}:
            self.disassemble_and_handle_byte_errors()
            
            trace_msg = 'Revised instructions (taking into account ' \
                        + 'potential byte misinterpretations):\n'
            for ins_address in common_objs.disassembled_firmware:
                instruction = common_objs.disassembled_firmware[ins_address]['insn']
                bytes = ''.join('{:02x}'.format(x) for x in instruction.bytes)
                trace_msg += '\t\t\t\t\t\t\t\t0x%x:\t%s\t%s\t%s\n' %(ins_address,
                                                bytes,
                                                instruction.mnemonic,
                                                instruction.op_str)
            logging.trace(trace_msg)
        
        # There's no need to disassemble again if app code base is 0.
        if common_objs.app_code_base > 0x00000000:
            logging.trace('Disassembling again due to non-zero code base.')
            self.disassemble_and_handle_byte_errors()
            
            trace_msg = 'Final disassembly (prior to inline data checks):\n'
            for ins_address in common_objs.disassembled_firmware:
                instruction = common_objs.disassembled_firmware[ins_address]['insn']
                bytes = ''.join('{:02x}'.format(x) for x in instruction.bytes)
                trace_msg += '\t\t\t\t\t\t\t\t0x%x:\t%s\t%s\t%s\n' %(ins_address,
                                                bytes,
                                                instruction.mnemonic,
                                                instruction.op_str)
            logging.trace(trace_msg)
            
        # Estimate architecture.
        self.test_arm_arch()
        disassembled_fw = None
        
    def disassemble_and_handle_byte_errors(self):
        disassembled_fw = self.disassemble_fw()
        common_objs.disassembled_firmware = disassembled_fw
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        common_objs.code_end_address = all_addresses[-1]
        self.handle_potential_misinterpretation_errors()
    
    def identify_inline_data(self):   
        logging.info('Identifying inline data.')
        
        # First mark out current code end address.
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        common_objs.code_end_address = all_addresses[-1]
        all_addresses = None

        # Get the vector table addresses.
        self.vector_table_addresses = []
        for intrpt in common_objs.application_vector_table:
            if intrpt == 'initial_sp':
                continue
            self.vector_table_addresses.append(
                common_objs.application_vector_table[intrpt]
            )
        # Add dummy keys, to handle Capstone issues.
        disassembled_firmware_with_dummy_keys = self.add_dummy_keys(
            common_objs.disassembled_firmware
        )
        
        # Now add firmware to common_objs.
        common_objs.disassembled_firmware = disassembled_firmware_with_dummy_keys
        disassembled_firmware_with_dummy_keys = None
        
        # See if any data values are being interpreted as instructions.
        self.check_data_instructions()
            
        # Remove dummy keys.
        common_objs.disassembled_firmware = self.remove_dummy_keys(
            common_objs.disassembled_firmware
        )
        
        # Check again for inline data, but this time using inline addresses.
        self.check_inline_address_instructions()

        # Trace message.
        logging.trace('Regenerating instructions.')
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        trace_msg = 'Final instructions: \n'
        address = common_objs.code_start_address - 2
        while address <= common_objs.code_end_address:
            address += 2
            if address not in common_objs.disassembled_firmware:
                continue
            if common_objs.disassembled_firmware[address]['is_data'] == True:
                next_address = utils.get_next_address(all_addresses, address)
                if next_address == None: next_address = address + 2
                data = utils.get_firmware_bytes(
                    address,
                    next_address-address
                )
                trace_msg += '\t\t\t\t\t\t\t\t0x%x:\t%s\t%s\t%s\n' %(address,
                                            data,
                                            'data',
                                            '')
            else:
                insn = common_objs.disassembled_firmware[address]['insn']
                bytes = insn.bytes
                bytes = ''.join('{:02x}'.format(x) for x in bytes)
                trace_msg += '\t\t\t\t\t\t\t\t0x%x:\t%s\t%s\t%s\n' %(address,
                                            bytes,
                                            insn.mnemonic,
                                            insn.op_str)
        logging.trace(trace_msg)
        
        self.vector_table_addresses = None
        
    def annotate_links(self):
        self.all_addresses = list(common_objs.disassembled_firmware.keys())
        self.all_addresses.sort()
        
        # Create backlinks.
        common_objs.disassembled_firmware = self.check_valid_branches(
            common_objs.disassembled_firmware
        )
        
        self.all_addresses = None
        
        # Mark out last known instruction.
        common_objs.disassembled_firmware = self.mark_last_instruction(
            common_objs.disassembled_firmware
        )
        
    def disassemble_fw(self):
        logging.info(
            'Disassembling firmware using Capstone '
            + 'using disassembly start address: '
            + hex(common_objs.disassembly_start_address)
        )
        
        disassembled_fw = {}
        with open(common_paths.path_to_fw, 'rb') as f:
            byte_file = f.read()
            # Save firmware bytes.
            common_objs.core_bytes = byte_file

        disassembled = md.disasm(
            byte_file,
            common_objs.disassembly_start_address
        )
        
        trace_msg = 'Disassembled firmware instructions:\n'
        for instruction in disassembled:
            disassembled_fw[instruction.address] = {
                'insn': instruction,
                'is_data': False
            }
            bytes = ''.join('{:02x}'.format(x) for x in instruction.bytes)
            trace_msg += '\t\t\t\t\t\t\t\t0x%x:\t%s\t%s\t%s\n' %(instruction.address,
                                            bytes,
                                            instruction.mnemonic,
                                            instruction.op_str)
        logging.trace(trace_msg)
        
        return disassembled_fw
        
    def add_dummy_keys(self, disassembled_fw):
        logging.debug('Creating dummy keys for disassembled object.')
        # Add dummy keys to the object, to prevent errors later.
        all_keys = list(disassembled_fw.keys())
        all_keys.sort()
        first_key = all_keys[0]
        last_key = all_keys[-1]
        disassembled_fw_with_dummy_keys = {}
        for index in range(first_key, last_key, 2):
            if index in disassembled_fw:
                disassembled_fw_with_dummy_keys[index] = disassembled_fw[index]
            else:
                disassembled_fw_with_dummy_keys[index] = {
                    'insn': None,
                    'is_data': False
                }
        
        return disassembled_fw_with_dummy_keys
        
    def remove_dummy_keys(self, disassembled_fw):
        new_fw = {}
        all_keys = list(disassembled_fw.keys())
        all_keys.sort()
        for ins_address in all_keys:
            if ((disassembled_fw[ins_address]['insn'] == None) and 
                    (disassembled_fw[ins_address]['is_data'] == False)):
                continue
            new_fw[ins_address] = disassembled_fw[ins_address]
        return new_fw
        
    def check_data_instructions(self):
        """Checks to see if any instructions are actually data values."""
        logging.debug(
            'Checking for presence of inline data (data as instructions).'
        )

        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        
        # Read in data from the Reset Handler.
        self.identify_data_segment_via_reset_handler()
        # Check for additional data segments using null bytes.
        # Maybe don't because some firmware files are split into sections.
        #self.estimate_end_of_app_code()
        logging.debug(
            'Code end address is '
            + hex(common_objs.code_end_address)
        )
        
        self.identify_switch_functions()
        
        ins_address = common_objs.code_start_address - 2
        while ins_address < common_objs.code_end_address:
            ins_address = utils.get_next_address(
                all_addresses,
                ins_address
            )
            if ins_address == None: break

            if ins_address in common_objs.errored_instructions:
                continue
  
            insn = common_objs.disassembled_firmware[ins_address]['insn']
            if insn == None:
                continue
   
            # If ID is 0, then it may mean inline data.
            if insn.id == ARM_INS_INVALID:
                ins_address = self.handle_data_byte(ins_address)
                continue
                
            # Handle incorrect IT instructions.
            if ((insn.id == ARM_INS_IT) and (insn.cc == ARM_CC_AL)):
                if 'e' in insn.mnemonic:
                    common_objs.disassembled_firmware[ins_address]['is_data'] = True
                    common_objs.disassembled_firmware[ins_address]['insn'] = None
                    
            # If it's a BL to ARM_SWITCH8:
            if insn.id == ARM_INS_BL:
                target_address_int = insn.operands[0].value.imm
                if target_address_int == self.arm_switch8:
                    ins_address = self.handle_data_switch8_table(ins_address)
                    continue
                if target_address_int in self.gnu_thumb:
                    subtype = common_objs.replace_functions[target_address_int]['subtype']
                    ins_address = self.handle_data_gnu_switch_table(ins_address, subtype)
                    continue
            
            # Table branch indices.
            if insn.id in [ARM_INS_TBB, ARM_INS_TBH]:
                ins_address = self.handle_data_table_branches(ins_address)
                continue
                
            # If the instruction is not a valid LDR instruction, then don't bother.
            if ((self.check_valid_pc_ldr(ins_address) == True) or (insn.id == ARM_INS_ADR)):
                ins_address = self.handle_data_ldr_adr(ins_address)
                continue
                
            # If the instruction writes to pc.
            if insn.id in [ARM_INS_LDR, ARM_INS_ADD, ARM_INS_MOV, 
                    ARM_INS_MOVT, ARM_INS_MOVW]:
                if insn.operands[0].value.reg == ARM_REG_PC:
                    if insn.operands[1].type == ARM_OP_REG:
                        src_reg = insn.operands[1].value.reg
                        if src_reg in [ARM_REG_LR, ARM_REG_SP]:
                            continue
                    elif insn.operands[1].type == ARM_OP_MEM:
                        src_reg = insn.operands[1].value.mem.base
                        if src_reg in [ARM_REG_LR, ARM_REG_SP]:
                            continue
                    ins_address = self.handle_data_pc(ins_address)
                    continue

    def handle_data_byte(self, ins_address):
        insn = common_objs.disassembled_firmware[ins_address]['insn']
        if ('byte' in insn.mnemonic):
            common_objs.errored_instructions.append(ins_address)
            logging.trace(
                '"byte" in mnemonic at '
                + hex(ins_address)
                + '. Adding to errored instructions.'
            )
            return ins_address
        common_objs.disassembled_firmware[ins_address]['is_data'] = True
        return ins_address

    def handle_data_switch8_table(self, ins_address):
        # Skip next few instructions.
        lr_value = ins_address+4
        switch_table_len_byte = utils.get_firmware_bytes(lr_value, 1)
        end_index = int(switch_table_len_byte, 16)
        table_branch_max = lr_value + end_index + 2
        if table_branch_max%2 == 1: 
            table_branch_max += 1
        logging.debug(
            'Call to ARM_Switch8 at '
            + hex(ins_address)
            + '. Skipping next few instructions to '
            + hex(table_branch_max)
        )
        
        if ins_address not in common_objs.replace_functions:
            common_objs.replace_functions[ins_address] = {
                'type': consts.FN_ARMSWITCH8CALL
            }
        else:
            return ins_address
        common_objs.replace_functions[ins_address]['table_branch_max'] = \
            table_branch_max
            
        # Get all possible addresses.
        table_branch_addresses = []
        switch8_index = lr_value
        while switch8_index < (table_branch_max-1):
            switch8_index += 1
            switch_table_index = utils.get_firmware_bytes(switch8_index, 1)
            (result,carry) = \
                binops.logical_shift_left(switch_table_index, 1)
            result_bin = utils.get_binary_representation(result, 8)
            result = str(carry) + result_bin
            switch8_address = lr_value + int(result, 2)
            table_branch_addresses.append(switch8_address)

        common_objs.replace_functions[ins_address]['table_branch_addresses'] = \
            table_branch_addresses
           
        table_branch_address_str = ''
        for table_branch_address in table_branch_addresses:
            table_branch_address_str += hex(table_branch_address)
            table_branch_address_str += ';'
            
        logging.debug(
            'ARM branch addresses: ' 
            + table_branch_address_str
        )     
        
        self.mark_table_as_data(lr_value, table_branch_max, 'ARM switch')
        
        ins_address = table_branch_max
        return ins_address
                
    def handle_data_gnu_switch_table(self, ins_address, subtype):
        insn = common_objs.disassembled_firmware[ins_address]['insn']
        
        # Get the value that is compared, the register that contains it,
        #  the address the comparison occurs at and the subsequent branch.
        (comp_value, comp_reg, comp_address, cbranch, cbranch_condition) = \
            self.get_preceding_comparison_branch(ins_address)
        if comp_value == None:
            ins_address += len(insn.bytes)
            return ins_address
            
        logging.trace(
            'GNU switch table at: '
            + hex(ins_address)
            + '. Comp value: '
            + str(comp_value)
            + '; comp register: '
            + str(comp_reg)
            + '; comp address: '
            + hex(comp_address)
            + '; comp branch address: '
            + hex(cbranch)
            + '; comp branch condition: '
            + str(cbranch_condition)
        )
        
        if ins_address not in common_objs.replace_functions:
            common_objs.replace_functions[ins_address] = {
                'type': consts.FN_GNUTHUMBCALL
            }
        else:
            return ins_address
            
        # In case the comparsion register is overwritten:            
        address = cbranch
        while address < ins_address:
            address += 2
            if utils.is_valid_code_address(address) != True:
                continue
            mov_insn = common_objs.disassembled_firmware[address]['insn']
            if mov_insn.id not in [ARM_INS_MOV, ARM_INS_MOVT, ARM_INS_MOVW]:
                continue
            if mov_insn.operands[0].value.reg != ARM_REG_R0:
                continue
            if mov_insn.operands[1].type == ARM_OP_IMM:
                comp_value = mov_insn.operands[1].value.imm
                
        # Align LR
        lr_address = ins_address + 4
        if subtype in ['case_sqi', 'case_uqi']:
            if lr_address%2 == 1: 
                lr_address -= 1
            mul_factor = 1
        elif subtype in ['case_shi', 'case_uhi']:
            if lr_address%2 == 1: 
                lr_address -= 1
            mul_factor = 2
        else:
            lr_address += 2
            rem = lr_address%4
            if rem > 0:
                lr_address -= rem
            mul_factor = 4

        num_entries = (comp_value + 1)
        size_table = num_entries * mul_factor
        common_objs.replace_functions[ins_address]['size_table'] = size_table
        
        table_branch_max = lr_address + size_table
        if subtype in ['case_sqi', 'case_uqi']:
            if (table_branch_max%2 == 1):
                table_byte = utils.get_firmware_bytes(table_branch_max, mul_factor)
                if table_byte == '00':
                    table_branch_max += 1
                else:
                    logging.error('Unhandled GNU Thumb')
                    table_branch_max += 1
                
        common_objs.replace_functions[ins_address]['table_branch_max'] = \
            table_branch_max
            
        logging.debug(
            'Skip GNU switch table  at '
            + hex(ins_address)
            + ' to '
            + hex(table_branch_max)
        )
            
        # Get all possible addresses.
        table_branch_addresses = []
        for i in range(num_entries):
            index_address = lr_address + (mul_factor*i)
            value = utils.get_firmware_bytes(
                index_address, 
                num_bytes=mul_factor
            )
            
            if (subtype in ['case_uqi', 'case_uhi']):
                value = value.zfill(8)
                value = int(value, 16)
            elif (subtype in ['case_sqi', 'case_shi']):
                value = binops.sign_extend(value)
                bin_value = utils.get_binary_representation(value, 32)
                if bin_value[0] == '1':
                    value = (4294967296 - int(value, 16)) * (-1)
                else:
                    value = int(value, 16)
            
            branch_address = lr_address + (2*value)
            if subtype == 'case_si':
                branch_address = lr_address + value
            table_branch_addresses.append(branch_address)
        
        common_objs.replace_functions[ins_address]['table_branch_addresses'] = \
            table_branch_addresses
           
        table_branch_address_str = ''
        for table_branch_address in table_branch_addresses:
            table_branch_address_str += hex(table_branch_address)
            table_branch_address_str += ';'
            
        logging.debug(
            'GNU branch addresses: ' 
            + table_branch_address_str
        )           
        
        self.mark_table_as_data(lr_address, table_branch_max, 'GNU switch')
        
        ins_address = table_branch_max
        return ins_address
        
    def handle_data_pc(self, ins_address):
        insn = common_objs.disassembled_firmware[ins_address]['insn']
        next_address = ins_address + len(insn.bytes)
        pc_address = ins_address + 4
        
        # Get the value that is compared, the register that contains it,
        #  the address the comparison occurs at and the subsequent branch.
        (comp_value, comp_reg, comp_address, cbranch, cbranch_condition) = \
            self.get_preceding_comparison_branch(ins_address)
        if comp_value == None:
            ins_address += len(insn.bytes)
            return ins_address
            
        logging.trace(
            'PC switch table at: '
            + hex(ins_address)
            + '. Comp value: '
            + str(comp_value)
            + '; comp register: '
            + str(comp_reg)
            + '; comp address: '
            + hex(comp_address)
            + '; comp branch address: '
            + hex(cbranch)
            + '; comp branch condition: '
            + str(cbranch_condition)
        )
        
        num_entries = (comp_value + 1)
        
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        trace_start = utils.get_next_address(all_addresses, cbranch)
        
        if common_objs.disassembled_firmware[trace_start]['insn'] == None:
            ins_address += len(insn.bytes)
            return ins_address
            
        if (common_objs.disassembled_firmware[trace_start]['insn'].id 
                in [ARM_INS_B, ARM_INS_BL, ARM_INS_BLX, ARM_INS_BX,
                    ARM_INS_CBZ, ARM_INS_CBNZ]):
            trace_start = utils.get_next_address(all_addresses, trace_start)
            
        # Identify the LDR instruction address, 
        #  so that we can identify LDR sources and mark them as data. 
        ldr_address = trace_start
        while ldr_address < ins_address:
            if ldr_address in common_objs.errored_instructions:
                ldr_address = utils.get_next_address(all_addresses, ldr_address)
                continue
            ldr_insn = common_objs.disassembled_firmware[ldr_address]
            if ldr_insn['insn'] == None:
                ldr_address = utils.get_next_address(all_addresses, ldr_address)
                continue
            # We don't care about PC-relevant LDR because we will have handled
            #  those already.
            if self.check_valid_pc_ldr(ldr_address) == True:
                ldr_address = utils.get_next_address(all_addresses, ldr_address)
                continue
            # If instruction is a LDR
            if (ldr_insn['insn'].id in [ARM_INS_LDR, ARM_INS_LDRB, ARM_INS_LDRH,
                    ARM_INS_LDRSB, ARM_INS_LDRSH]):
                break
            ldr_address = utils.get_next_address(all_addresses, ldr_address)
        
        if ldr_address == ins_address: 
            logging.error('No LDR instruction')
            ins_address = utils.get_next_address(all_addresses, ins_address)
            return ins_address

        ldr_insn = common_objs.disassembled_firmware[ldr_address]['insn']
        ldr_operands = ldr_insn.operands
        base_register = ldr_operands[1].value.mem.base
        if base_register in [ARM_REG_LR, ARM_REG_SP]:
            logging.warning(
                'Unsupported PC switch (LR/SP) at ' 
                + hex(ins_address)
            )
            ins_address = utils.get_next_address(all_addresses, ins_address)
            return ins_address
            
        ldr_size = 1
        if ldr_insn.id in [ARM_INS_LDRSH, ARM_INS_LDRH]:
            ldr_size = 2
        elif ldr_insn.id == ARM_INS_LDR:
            ldr_size = 4
        post_index_reg = None
        if len(ldr_operands) == 3:
            post_index_reg = ldr_operands[2]
                
        logging.debug('Tracing for PC switch at ' + hex(ins_address))
        ldr_trace_end = utils.get_previous_address(all_addresses, ldr_address)
        for i in range(num_entries):
            logging.trace('Tracing for PC switch LDRs with index ' + str(i))
            (strand_exec_inst, init_regs, condition_flags, current_path) = \
                self.initialise_objects_for_trace(
                    all_addresses,
                    trace_start, 
                    comp_reg, 
                    i
                )
            
            # Trace LDR using register evaluator.
            (_, _, register_object) = \
                strand_exec_inst.trace_register_values(
                    common_objs.disassembled_firmware,
                    trace_start, [ldr_trace_end],   
                    init_regs, {}, condition_flags, True
                )
            (src_memory_address, _) = \
                strand_exec_inst.get_memory_address(
                    register_object,
                    ldr_address,
                    ldr_operands[1],
                    ldr_insn.writeback,
                    post_index_reg
                )
            if src_memory_address == None:
                logging.debug(
                    'Unable to compute PC LDR address. '
                    + 'Skipping.'
                )
                ins_address = utils.get_next_address(all_addresses, ins_address)
                return ins_address
                
            logging.debug(
                'Marking '
                + hex(src_memory_address)
                + ' as PC LDR address (switch table).'
            )
            if src_memory_address%2 == 1: 
                src_memory_address -= 1
            common_objs.disassembled_firmware[src_memory_address]['is_data'] = True
            common_objs.disassembled_firmware[src_memory_address]['insn'] = None
            if ldr_insn.id == ARM_INS_LDR:
                common_objs.disassembled_firmware[src_memory_address+2]['is_data'] = True
                common_objs.disassembled_firmware[src_memory_address+2]['insn'] = None
            strand_exec_inst = None
            
        # Everything needs to be re-initialised, so just do this separately.
        table_branch_addresses = []
        for i in range(num_entries):
            logging.trace('Tracing for PC switch table entries with index ' + str(i))
            (strand_exec_inst, init_regs, condition_flags, current_path) = \
                self.initialise_objects_for_trace(
                    all_addresses,
                    trace_start, 
                    comp_reg, 
                    i
                )
            # Get PC value.
            (_, _, register_object) = \
                strand_exec_inst.trace_register_values(
                    common_objs.disassembled_firmware,
                    trace_start, [ins_address],   
                    init_regs, {}, condition_flags, True
                )
            
            pc_value = int(register_object[ARM_REG_PC], 16)
            table_branch_addresses.append(pc_value)
            
            strand_exec_inst = None

        if ins_address not in common_objs.replace_functions:
            common_objs.replace_functions[ins_address] = {
                'type': consts.PC_SWITCH
            }
        else:
            return ins_address
        common_objs.replace_functions[ins_address]['table_branch_addresses'] = \
            table_branch_addresses
            
        table_branch_max = max(table_branch_addresses)
        common_objs.replace_functions[ins_address]['table_branch_max'] = \
            table_branch_max
            
        table_branch_address_str = ''
        for table_branch_address in table_branch_addresses:
            table_branch_address_str += hex(table_branch_address)
            table_branch_address_str += ';'
            
        logging.debug(
            'PC switch branch addresses: ' 
            + table_branch_address_str
        )
        ins_address = utils.get_next_address(all_addresses, ins_address)
        return ins_address
        
    def handle_data_table_branches(self, ins_address):
        insn = common_objs.disassembled_firmware[ins_address]['insn']
        index_register = insn.operands[0].value.mem.index

        # Get the value that is compared, the register that contains it,
        #  the address the comparison occurs at and the subsequent branch.
        (comp_value, comp_reg, comp_address, cbranch, cbranch_condition) = \
            self.get_preceding_comparison_branch(ins_address)
        if comp_value == None:
            logging.trace(
                'Comp value returned None for table branch at :'
                + hex(ins_address)
            )
            ins_address += len(insn.bytes)
            return ins_address

        logging.trace(
            'Table branch switch table at: '
            + hex(ins_address)
            + '. Comp value: '
            + str(comp_value)
            + '; comp register: '
            + str(comp_reg)
            + '; comp address: '
            + hex(comp_address)
            + '; comp branch address: '
            + hex(cbranch)
            + '; comp branch condition: '
            + str(cbranch_condition)
        )
        
        comparison_reg = index_register
        if comparison_reg != comp_reg:
            ins_address += len(insn.bytes)
            return ins_address
            
        if ins_address not in common_objs.table_branches:
            common_objs.table_branches[ins_address] = {}
        common_objs.table_branches[ins_address]['comparison_value'] = \
            comp_value
        common_objs.table_branches[ins_address]['comparison_address'] = \
            comp_address
        common_objs.table_branches[ins_address]['comparison_register'] = \
            comp_reg
        common_objs.table_branches[ins_address]['branch_address'] = \
            cbranch
        common_objs.table_branches[ins_address]['branch_condition'] = \
            cbranch_condition
        
        num_entries = (comp_value + 1)
        if insn.id == ARM_INS_TBB:
            mul_factor = 1
        if insn.id == ARM_INS_TBH:
            mul_factor = 2
        size_table = num_entries * mul_factor
        common_objs.table_branches[ins_address]['size_table'] = size_table
        
        pc_address = ins_address + 4
        table_branch_max = pc_address + size_table
        if insn.id == ARM_INS_TBB:
            if (table_branch_max%2 == 1):
                table_byte = utils.get_firmware_bytes(table_branch_max, 1)
                if table_byte == '00':
                    table_branch_max += 1
                else:
                    logging.error('Unhandled TBB at ' + hex(ins_address))
                    table_branch_max += 1
        
        common_objs.table_branches[ins_address]['table_branch_max'] = \
            table_branch_max
            
        logging.debug(
            'Skip TBB/TBH at '
            + hex(ins_address)
            + ' to '
            + hex(table_branch_max)
        )
            
        # Get all possible addresses.
        table_branch_addresses = []
        for i in range(comp_value+1):
            index_address = pc_address + (mul_factor*i)
            value = utils.get_firmware_bytes(
                index_address, 
                num_bytes=mul_factor
            )
            value = int(value, 16)
            branch_address = pc_address + (2*value)
            table_branch_addresses.append(branch_address)
        common_objs.table_branches[ins_address]['table_branch_addresses'] = \
            table_branch_addresses
        
        self.mark_table_as_data(pc_address, table_branch_max, 'table branch')
        
        return ins_address
    
    def get_preceding_comparison_branch(self, ins_address):
        # Get comparison value.
        address = ins_address
        comp_address = None
        comp_value = None
        for i in range(10):
            address -= 2
            if utils.is_valid_code_address(address) != True:
                continue
            prev_insn = common_objs.disassembled_firmware[address]['insn']
            if prev_insn.id != ARM_INS_CMP:
                continue
            comp_value = prev_insn.operands[1].value.imm
            comp_reg = prev_insn.operands[0].value.reg
            comp_address = address
            break
        
        if comp_address == None:
            return (None, None, None, None, None)
            
        cbranch = comp_address
        cbranch_address = None
        cbranch_condition = None
        while cbranch < (ins_address-2):
            cbranch += 2
            if cbranch == ins_address: break
            if utils.is_valid_code_address(cbranch) != True:
                continue
            branch_insn = common_objs.disassembled_firmware[cbranch]['insn']
            if branch_insn.id not in [ARM_INS_B, ARM_INS_IT]:
                continue
            cbranch_address = cbranch
            if branch_insn.cc in [ARM_CC_AL, ARM_CC_INVALID]:
                continue
            cbranch_condition = branch_insn.cc

        if cbranch_condition in [ARM_CC_HS]:
            comp_value -= 1
            
        if cbranch_address == None:
            comp_value = None

        return (comp_value, comp_reg, comp_address, cbranch_address, cbranch_condition)
        
    def mark_table_as_data(self, data_start_address, next_nondata, struct_name):
        original_bytes = None
        while data_start_address < next_nondata:
            logging.debug(
                'Marking '
                + hex(data_start_address)
                + ' as '
                + struct_name
                + ' index table.'
            )
            # Get the original bytes, as we may need to re-disassemble.
            if common_objs.disassembled_firmware[data_start_address]['insn'] != None:
                original_bytes = \
                    common_objs.disassembled_firmware[data_start_address]['insn'].bytes
            else:
                original_bytes = b''
            common_objs.disassembled_firmware[data_start_address]['is_data'] = True
            common_objs.disassembled_firmware[data_start_address]['_insn'] = \
                common_objs.disassembled_firmware[data_start_address]['insn']
            common_objs.disassembled_firmware[data_start_address]['insn'] = None
            data_start_address += 2
            
        if len(original_bytes) == 4:
            new_bytes = utils.get_firmware_bytes(data_start_address, 2)
            new_bytes = bytes.fromhex(new_bytes)
            new_insns = md.disasm(
                new_bytes,
                data_start_address
            )
            for new_insn in new_insns:
                logging.debug(
                    'Re-processing instruction at '
                    + hex(new_insn.address)
                )
                common_objs.disassembled_firmware[new_insn.address] = {
                    'insn': new_insn,
                    'is_data': False
                }
    
    def handle_data_ldr_adr(self, ins_address):
        insn = common_objs.disassembled_firmware[ins_address]['insn']
        curr_pc_value = self.get_mem_access_pc_value(ins_address)
        operands = insn.operands
        
        # If ADR is loading to registers other than R0-R2,
        #  then don't use it for inline data identification?
        # Hack to reduce FPs.
        if (insn.id == ARM_INS_ADR):
            if operands[0].value.reg not in [ARM_REG_R0, ARM_REG_R1, ARM_REG_R2]:
                return ins_address
        
        # Target address is PC + offset.
        ldr_target = curr_pc_value + operands[1].mem.disp
        if insn.id == ARM_INS_ADR:
            ldr_target = curr_pc_value + operands[1].value.imm
                    
        outcome = self.process_data_addresses(ins_address, ldr_target, insn.id)
        if outcome == consts.ERROR_INVALID_INSTRUCTION:
            if ins_address not in common_objs.errored_instructions:
                common_objs.errored_instructions.append(ins_address)
                logging.trace(
                    'Unable to load data bytes for LDR call at '
                    + hex(ins_address)
                    + '. Adding to errored instructions.'
                )
            return ins_address

        return ins_address
                
    def handle_potential_misinterpretation_errors(self):
        logging.trace('Checking for byte misinterpretations.')
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()

        ins_address = 0x3c + common_objs.app_code_base
        address_end = all_addresses[-1]
        while ins_address <= address_end:
            ins_address = utils.get_next_address(
                all_addresses,
                ins_address
            )
            if ins_address == None: break
                
            insn = common_objs.disassembled_firmware[ins_address]['insn']
            if insn == None: continue
            if insn.id == 0: continue
            
            # Handle incorrect bytes.
            # Capstone seems to misinterpret most when 'ff' is in the byte array.
            bytes = ''.join('{:02x}'.format(x) for x in insn.bytes)
            if ((insn.mnemonic.startswith('v')) 
                    and (len(insn.bytes) == 4)):
                self.handle_misinterpretation(ins_address, insn)
    
    def is_byte_specific_invalid_or_nop(self, address):
        if utils.is_valid_code_address(address) != True:
            return True
        if 'temp_data' in common_objs.disassembled_firmware[address]:  
            if common_objs.disassembled_firmware[address]['temp_data'] == True:
                return True
        insn = common_objs.disassembled_firmware[address]['insn']
        if insn.id  == ARM_INS_NOP:
            return True
        if insn.id in [ARM_INS_MOV, ARM_INS_MOVT, ARM_INS_MOVW]:
            if insn.operands[0].value.reg == insn.operands[1].value.reg:
                return True
        return False
    
    def handle_misinterpretation(self, ins_address, insn):
        logging.debug('Handling potential incorrect insn at ' + hex(ins_address))
        insn_bytes = common_objs.disassembled_firmware[ins_address]['insn'].bytes
        insn = md.disasm(
            insn_bytes[0:2], 
            ins_address
        )
        for code_start_insn in insn:
            if code_start_insn.address not in common_objs.disassembled_firmware:
                common_objs.disassembled_firmware[code_start_insn.address] = {}
            common_objs.disassembled_firmware[code_start_insn.address]['insn'] = \
                code_start_insn
            common_objs.disassembled_firmware[code_start_insn.address]['is_data'] = False
            logging.trace(
                'New instruction at ' 
                + hex(code_start_insn.address)
                + " "
                + code_start_insn.mnemonic
            )
        if len(insn_bytes) == 2: return
        
        insn2 = md.disasm(
            insn_bytes[2:4], 
            ins_address+2
        )
        for code_start_insn in insn2:
            if code_start_insn.address not in common_objs.disassembled_firmware:
                common_objs.disassembled_firmware[code_start_insn.address] = {}
            common_objs.disassembled_firmware[code_start_insn.address]['insn'] = \
                code_start_insn
            common_objs.disassembled_firmware[code_start_insn.address]['is_data'] = False
            logging.trace(
                'New instruction at ' 
                + hex(code_start_insn.address)
                + " "
                + code_start_insn.mnemonic
            )

        if ins_address + 4 in common_objs.disassembled_firmware:
            if common_objs.disassembled_firmware[ins_address+4]['insn'] == None:
                return
            next_insn_bytes = common_objs.disassembled_firmware[ins_address+4]['insn'].bytes
            subsequent_bytes = None
            if len(next_insn_bytes) == 4:
                subsequent_bytes = next_insn_bytes[2:4]
                next_insn_bytes = next_insn_bytes[0:2]
            next_insn = md.disasm(
                insn_bytes[2:4] + next_insn_bytes, 
                ins_address+2
            )
            for code_start_insn in next_insn:
                if code_start_insn.address not in common_objs.disassembled_firmware:
                    common_objs.disassembled_firmware[code_start_insn.address] = {}
                common_objs.disassembled_firmware[code_start_insn.address]['insn'] = \
                    code_start_insn
                common_objs.disassembled_firmware[code_start_insn.address]['is_data'] = False
                logging.trace(
                    'New instruction at ' 
                    + hex(code_start_insn.address)
                    + " "
                    + code_start_insn.mnemonic
                )
            if subsequent_bytes != None:
                next_insn = md.disasm(
                    subsequent_bytes, 
                    ins_address+6
                )
                for code_start_insn in next_insn:
                    if code_start_insn.address not in common_objs.disassembled_firmware:
                        common_objs.disassembled_firmware[code_start_insn.address] = {}
                    common_objs.disassembled_firmware[code_start_insn.address]['insn'] = \
                        code_start_insn
                    common_objs.disassembled_firmware[code_start_insn.address]['is_data'] = False
                    logging.trace(
                        'New instruction at ' 
                        + hex(code_start_insn.address)
                        + " "
                        + code_start_insn.mnemonic
                    )
                    break
    
    def identify_switch_functions(self):
        """ Identify __ARM_common_switch8 and the __gnu_thumb1 variants."""
        self.identify_arm_switch8()
        self.identify_gnu_switch()
        
    def identify_arm_switch8(self):
        logging.debug('Checking for __ARM_common_switch8')
        arm_switch8 = None
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        ins_address = common_objs.code_start_address - 2
        while ins_address <= common_objs.code_end_address:
            ins_address += 2
            if utils.is_valid_code_address(ins_address) != True:
                continue
            insn = common_objs.disassembled_firmware[ins_address]['insn']
            is_potential_arm_switch8 = False
            if insn.id == ARM_INS_PUSH:
                operands = insn.operands
                if len(operands) == 2:
                    if ((operands[0].value.reg == ARM_REG_R4) 
                            and (operands[1].value.reg == ARM_REG_R5)):
                        is_potential_arm_switch8 = True
            if is_potential_arm_switch8 != True:
                continue
            if ((ins_address+2) not in common_objs.disassembled_firmware):
                is_potential_arm_switch8 = False
                continue
            next_insn = common_objs.disassembled_firmware[ins_address+2]['insn']
            if next_insn == None:
                is_potential_arm_switch8 = False
                continue
            if next_insn.id not in [ARM_INS_MOV, ARM_INS_MOVT, ARM_INS_MOVW]:
                is_potential_arm_switch8 = False
                continue
            next_operands = next_insn.operands
            if next_operands[0].value.reg != ARM_REG_R4:
                is_potential_arm_switch8 = False
                continue
            if next_operands[1].value.reg != ARM_REG_LR:
                is_potential_arm_switch8 = False
                continue
            if is_potential_arm_switch8 == True:
                arm_switch8 = ins_address
                break
        if arm_switch8 == None: return
        logging.info('ARM switch8 identified at ' + hex(arm_switch8))
        self.arm_switch8 = arm_switch8
        common_objs.replace_functions[arm_switch8] = {
            'type': consts.FN_ARMSWITCH8
        }
    
    def identify_gnu_switch(self):
        logging.debug('Checking for __gnu_thumb1 variants')
        gnu_thumb = None
        self.gnu_thumb = []
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        ins_address = common_objs.code_start_address - 2
        while ins_address <= common_objs.code_end_address:
            ins_address += 2
            if utils.is_valid_code_address(ins_address) != True:
                continue
                
            insn = common_objs.disassembled_firmware[ins_address]['insn']
            
            is_potential_gnu_thumb = False
            if insn.id == ARM_INS_PUSH:
                operands = insn.operands
                if len(operands) == 2:
                    if ((operands[0].value.reg == ARM_REG_R0) 
                            and (operands[1].value.reg == ARM_REG_R1)):
                        is_potential_gnu_thumb = True
                elif len(operands) == 1:
                    if (operands[0].value.reg == ARM_REG_R1):
                        is_potential_gnu_thumb = True
            if is_potential_gnu_thumb != True:
                continue
            if ((ins_address+2) not in common_objs.disassembled_firmware):
                is_potential_gnu_thumb = False
                continue
            next_insn = common_objs.disassembled_firmware[ins_address+2]['insn']
            if next_insn == None:
                is_potential_gnu_thumb = False
                continue
            if next_insn.id not in [ARM_INS_MOV, ARM_INS_MOVT, ARM_INS_MOVW]:
                is_potential_gnu_thumb = False
                continue
            next_operands = next_insn.operands
            if next_operands[0].value.reg != ARM_REG_R1:
                is_potential_gnu_thumb = False
                continue
            if next_operands[1].value.reg != ARM_REG_LR:
                is_potential_gnu_thumb = False
                continue
            if is_potential_gnu_thumb == True:
                gnu_thumb = ins_address
                ins_address += 2
                # There are 5 variants.
                subtype = None
                for i in range(6):
                    next_address = ins_address + 2*i
                    if (next_address not in common_objs.disassembled_firmware):
                        is_potential_gnu_thumb = False
                        continue
                    gnu_insn = common_objs.disassembled_firmware[next_address]['insn']
                    if gnu_insn == None:
                        is_potential_gnu_thumb = False
                        break
                    if gnu_insn.id == ARM_INS_LDRSB:
                        subtype = 'case_sqi'
                        break
                    if gnu_insn.id == ARM_INS_LDRB:
                        subtype = 'case_uqi'
                        break
                    if gnu_insn.id == ARM_INS_LDRSH:
                        subtype = 'case_shi'
                        break
                    if gnu_insn.id == ARM_INS_LDRH:
                        subtype = 'case_uhi'
                        break
                    if gnu_insn.id == ARM_INS_LDR:
                        subtype = 'case_si'
                        break
                if subtype == None:
                    is_potential_gnu_thumb = False
                    continue
                
                logging.info('GNU switch function identified at ' + hex(gnu_thumb))
                self.gnu_thumb.append(gnu_thumb)
                common_objs.replace_functions[gnu_thumb] = {
                    'type': consts.FN_GNUTHUMB,
                    'subtype': subtype
                }

    def identify_data_segment_via_reset_handler(self):
        reset_handler_address = common_objs.application_vector_table['reset']
        address = reset_handler_address - 2
        max_address = address + 30
        data_start_firmware_address = ''
        data_start_real_address = ''
        while address < max_address:
            address += 2
            insn = common_objs.disassembled_firmware[address]['insn']
            if insn == None:
                continue
            
            # If a self-targeting branch is encountered, we've probably
            #  come to another interrupt handler.
            if insn.id == ARM_INS_B:
                if insn.cc == ARM_CC_AL:
                    branch_target = insn.operands[0].value.imm
                    if branch_target < common_objs.code_start_address:
                        logging.trace(
                            'Branch target ('
                            + hex(branch_target)
                            + ') is not less than code start address '
                            + 'for call at '
                            + hex(address)
                            + '. Adding to errored instructions.'
                        )
                        common_objs.errored_instructions.append(address)
                        continue
                    if branch_target == address:
                        break
                        
            # If there's inline data, we've probably come to the end.
            if insn.id == ARM_INS_INVALID:
                if ('byte' in insn.mnemonic):
                    common_objs.errored_instructions.append(address)
                    logging.trace(
                        '"byte" in mnemonic at '
                        + hex(address)
                        + '. Adding to errored instructions.'
                    )
                    continue
                common_objs.disassembled_firmware[address]['is_data'] = True
                break
            if common_objs.disassembled_firmware[address]['is_data'] == True:
                break
                
            if self.check_valid_pc_ldr(address) != True:
                continue
                
            if insn.id == ARM_INS_LDR:
                curr_pc_value = self.get_mem_access_pc_value(address)
                
                # Target address is PC + offset.
                operands = insn.operands
                ldr_target = curr_pc_value + operands[1].mem.disp
                ldr_value = utils.get_firmware_bytes(ldr_target, 4)
                ldr_value = int(ldr_value, 16)
                outcome = self.process_data_addresses(address, ldr_target, insn.id)
                if outcome == consts.ERROR_INVALID_INSTRUCTION:
                    if ldr_address not in common_objs.errored_instructions:
                        common_objs.errored_instructions.append(ldr_address)
                        logging.trace(
                            'Unable to mark data bytes for load instruction at '
                            + hex(ldr_address)
                            + '. Adding to errored instructions.'
                        )
                    continue
                    
                if ldr_value in common_objs.disassembled_firmware:
                    if ldr_value < common_objs.code_start_address:
                        continue
                    if data_start_firmware_address == '':
                        data_start_firmware_address = ldr_value
                    else:
                        # We will use the largest value as start address.
                        if ldr_value < data_start_firmware_address:
                            continue
                        data_start_firmware_address = ldr_value
                    logging.debug(
                        'Possible start of .data is at: ' 
                        + hex(ldr_value)
                    )
                else:
                    # The LDR from address in firmware precedes LDR from RAM.
                    if data_start_firmware_address == '':
                        continue
                    data_start_real_address = ldr_value
                    logging.debug(
                        'Possible start address for .data: ' 
                        + hex(ldr_value)
                    )
            if ((data_start_firmware_address != '') and (data_start_real_address != '')):
                break
        if data_start_firmware_address == '':
            return
        if data_start_real_address == '':
            return
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        last_address = all_addresses[-1]
        all_addresses = None
        if data_start_firmware_address >= last_address:
            return

        data_region = {}
        fw_address = data_start_firmware_address
        real_address = data_start_real_address
        common_objs.data_segment_start_address = real_address
        common_objs.data_segment_start_firmware_address = fw_address
        while fw_address <= last_address:
            if real_address % 4 == 0:
                data_region_value = \
                    utils.get_firmware_bytes(
                        fw_address, 
                        4,
                        endian='big'
                    )
                data_region[real_address] = data_region_value
                common_objs.disassembled_firmware[fw_address]['data'] = \
                    int(data_region_value, 16)
            common_objs.disassembled_firmware[fw_address]['is_data'] = True
            real_address += 2
            fw_address += 2
        common_objs.data_region = data_region
        logging.debug(common_objs.data_region)
        
        # Mark code end address.
        potential_code_end = data_start_firmware_address - 2
        if ((common_objs.disassembled_firmware[potential_code_end]['insn'] == None)
                and (common_objs.disassembled_firmware[potential_code_end]['is_data'] == False)):
            potential_code_end -= 2
        common_objs.code_end_address = potential_code_end
    
    def estimate_end_of_app_code(self):
        logging.trace('Estimating end of app code.')
        start_of_code = common_objs.code_start_address-common_objs.app_code_base
        app_code_bytes = common_objs.core_bytes[start_of_code:]
        code_split = app_code_bytes.split(
            bytearray.fromhex('0000000000000000000000000000000000000000000000000000000000000000')
        )
        for single_split in code_split:
            if single_split == b'':
                continue
            else:
                first_split = single_split
                break
        length_first_split = len(first_split)
        if length_first_split%2 == 1: length_first_split += 1
        address_data_start = common_objs.code_start_address + length_first_split

        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        max_address = all_addresses[-1]
        
        if ((common_objs.code_end_address > 0) 
                and (common_objs.code_end_address < max_address)):
            max_address = common_objs.code_end_address
    
        if address_data_start > max_address:
            logging.debug('No data section identified.')
            return
            
        address = address_data_start
        logging.debug(
            'Marking addresses from '
            + hex(address_data_start)
            + ' as containing data.'
        )
        while address < max_address:
            common_objs.disassembled_firmware[address]['is_data'] = True
            common_objs.disassembled_firmware[address]['_insn'] = \
                common_objs.disassembled_firmware[address]['insn']
            common_objs.disassembled_firmware[address]['insn'] = None
            address += 2

        # Mark code end address
        potential_code_end = address_data_start - 2
        if potential_code_end not in common_objs.disassembled_firmware:
            potential_code_end -= 2
        if ((common_objs.disassembled_firmware[potential_code_end]['insn'] == None)
                and (common_objs.disassembled_firmware[potential_code_end]['is_data'] == False)):
            potential_code_end -= 2
        if potential_code_end < common_objs.code_end_address:
            common_objs.code_end_address = potential_code_end
        
    def check_valid_pc_ldr(self, ins_address):
        if ins_address not in common_objs.disassembled_firmware:
            return False
        insn = common_objs.disassembled_firmware[ins_address]['insn']
        if insn == None: return False
        
        if (insn.id not in [ARM_INS_LDR, ARM_INS_LDRB, ARM_INS_LDRH,
                ARM_INS_LDRSB, ARM_INS_LDRSH]):
            return False
            
        operands = insn.operands
        if len(operands) < 2:
            logging.error(
                'Unexpected number of operands for ldr instruction at address: '
                + hex(ins_address)
            )
            return False
            
        # If it's not PC-relative address, return false.
        if operands[1].value.reg != ARM_REG_PC:
            return False
        
        return True
            
    def get_ldr_target_data_bytes(self, ldr_target, num_bytes):
        try:
            data_bytes = utils.get_firmware_bytes(ldr_target, num_bytes)
        except:
            return consts.ERROR_INVALID_INSTRUCTION
        return data_bytes
    
    def process_data_addresses(self, ins_address, ldr_target, opcode_id):
        num_bytes = 1
        if opcode_id in [ARM_INS_LDRH, ARM_INS_LDRSH]:
            num_bytes = 2
        elif opcode_id in [ARM_INS_LDR, ARM_INS_ADR]:
            num_bytes = 4
        if ((num_bytes == 1) and (ldr_target%2 == 1)):
            ldr_target -= 1
        if ldr_target%2 == 1:
            return consts.ERROR_INVALID_INSTRUCTION
        logging.debug(
            'Marking '
            + hex(ldr_target)
            + ' as data called from '
            + hex(ins_address)
        )
        if ldr_target not in common_objs.disassembled_firmware:
            common_objs.disassembled_firmware[ldr_target] = {}
        common_objs.disassembled_firmware[ldr_target]['is_data'] = True
        common_objs.disassembled_firmware[ldr_target]['insn'] = None
        if num_bytes <= 2:
            return True
        logging.debug(
            'Marking '
            + hex(ldr_target+2)
            + ' as data called from '
            + hex(ins_address)
        )
        if (ldr_target+2) not in common_objs.disassembled_firmware:
            common_objs.disassembled_firmware[ldr_target+2] = {}
        common_objs.disassembled_firmware[ldr_target+2]['is_data'] = True
        common_objs.disassembled_firmware[ldr_target+2]['insn'] = None
        # Now we need to re-process the next instruction,
        #  but only if it doesn't already exist.
        if (ldr_target+4) in common_objs.disassembled_firmware:
            return True
            
        logging.debug(
            'Re-processing instruction at '
            + hex(ldr_target+4)
        )
        new_bytes = utils.get_firmware_bytes(ldr_target+4, 2)
        new_bytes = bytes.fromhex(new_bytes)
        new_insns = md.disasm(
            new_bytes,
            ldr_target+4
        )
        for new_insn in new_insns:
            logging.debug(
                'Re-processing instruction at '
                + hex(new_insn.address)
            )
            common_objs.disassembled_firmware[new_insn.address] = {
                'insn': new_insn,
                'is_data': False
            }
        return True
    
    def check_inline_address_instructions(self):
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        min_address = common_objs.code_start_address
        max_address = common_objs.code_end_address
        ins_address = common_objs.code_start_address - 2
        logging.debug(
            'Checking for presence of inline addresses '
            + 'starting from '
            + hex(min_address)
            + ' and ending '
            + hex(max_address)
        )
        while ins_address < common_objs.code_end_address:
            ins_address = utils.get_next_address(
                all_addresses,
                ins_address
            )
            if ins_address == None: break
            
            if utils.is_valid_code_address(ins_address) != True:
                continue
            insn = common_objs.disassembled_firmware[ins_address]['insn']
                    
            # If the instruction is not a valid LDR instruction, then don't bother.
            if self.check_valid_pc_ldr(ins_address) != True:
                continue
            if insn.id != ARM_INS_LDR:
                continue
            curr_pc_value = self.get_mem_access_pc_value(ins_address)

            # Target address is PC + offset.
            operands = insn.operands
            ldr_target = curr_pc_value + operands[1].mem.disp
            target_bytes = utils.get_firmware_bytes(ldr_target, 4)
            if target_bytes == '':
                if ins_address not in common_objs.errored_instructions:
                    common_objs.errored_instructions.append(ins_address)
                logging.trace(
                    'LDR bytes not present for LDR instruction at '
                    + hex(ins_address)
                )
                continue
            ordered_bytes = int(target_bytes, 16)

            # If it's an LDR instruction, then the bytes themselves may 
            #  represent an address within the instructions.
            if ((ordered_bytes >= min_address) and (ordered_bytes <= max_address)):
                is_target_address_data = False
                ldr_target_register = insn.operands[0].value.reg
                test_address = ins_address
                for i in range(5):
                    test_address = utils.get_next_address(
                        all_addresses,
                        test_address
                    )
                    if utils.is_valid_code_address(test_address) != True:
                        continue
                    test_insn = common_objs.disassembled_firmware[test_address]['insn']
                    # If the value loaded in register gets used in 
                    #  register-relative LDR, then the address is marked 
                    #  as containing data.
                    if test_insn.id == ARM_INS_LDR:
                        ldr_operand = test_insn.operands[1]
                        if ldr_operand.value.mem.base == ldr_target_register:
                            if ((ldr_operand.value.mem.index == 0) 
                                    and (ldr_operand.value.mem.disp == 0)
                                    and (ldr_operand.value.mem.lshift == 0)):
                                is_target_address_data = True
                                break
                    elif test_insn.id == ARM_INS_BX:
                        if test_insn.operands[0].value.reg == ldr_target_register:
                            is_target_address_data = True
                            break
                    # If the value loaded in register gets overwritten, 
                    #  don't continue.
                    if len(test_insn.operands) > 0:
                        if test_insn.operands[0].value.reg == ldr_target_register:
                            break
                if is_target_address_data != True:
                    continue
                inline_address = ordered_bytes
                if inline_address in common_objs.disassembled_firmware:
                    logging.debug(
                        'Marking inline address as data '
                        + hex(inline_address)
                        + ' as called from '
                        + hex(ins_address)
                    )
                    common_objs.disassembled_firmware[inline_address]['is_data'] = True
                    common_objs.disassembled_firmware[inline_address]['insn'] = None
                    if (inline_address+2) not in common_objs.disassembled_firmware:
                        common_objs.disassembled_firmware[inline_address+2] = {}
                    common_objs.disassembled_firmware[inline_address+2]['is_data'] = True
                    common_objs.disassembled_firmware[inline_address+2]['insn'] = None
                    
    # ------------------------------------------------------
    def check_valid_branches(self, disassembled_fw):
        logging.debug(
            'Checking basic branches and creating backlinks.'
        )
        for ins_address in disassembled_fw:
            if ins_address > common_objs.code_end_address:
                break
            if utils.is_valid_code_address(ins_address) != True:
                continue
                
            opcode_id = disassembled_fw[ins_address]['insn'].id
            
            # Check whether the opcode is for a branch instruction at all.
            if opcode_id not in [ARM_INS_BL, ARM_INS_B]:
                continue
                
            # Create backlink.
            disassembled_fw = self.create_backlink(
                disassembled_fw,
                ins_address
            )
        return disassembled_fw
                
    def create_backlink(self, disassembled_fw, ins_address):
        if utils.is_valid_code_address(ins_address) != True:
            return disassembled_fw
            
        insn = disassembled_fw[ins_address]['insn']
            
        if insn.id in [ARM_INS_BL, ARM_INS_B]:
            branch_address = insn.operands[0].value.imm
        elif insn.id in [ARM_INS_BLX, ARM_INS_BX]:
            branch_register = insn.operands[0].value.reg
            if branch_register == ARM_REG_LR:
                return disassembled_fw
            prev_insn_address = utils.get_previous_address(
                self.all_addresses,
                ins_address
            )
            if self.check_valid_pc_ldr(prev_insn_address) != True:
                return disassembled_fw
            prev_insn = disassembled_fw[prev_insn_address]['insn']
            if prev_insn.id != ARM_INS_LDR:
                return disassembled_fw
            curr_pc_value = self.get_mem_access_pc_value(prev_insn_address)
            operands = prev_insn.operands
            ldr_dst_reg = operands[0].value.reg
            if ldr_dst_reg != branch_register:
                return disassembled_fw
            ldr_target = curr_pc_value + operands[1].mem.disp
            branch_address = int(utils.get_firmware_bytes(ldr_target, 4), 16)
            if branch_address%2 == 1:
                branch_address -= 1
        
        # The address should already be present in our disassembled firmware.
        if branch_address not in disassembled_fw:
            # If the address is not in disassembled firmware, add it to
            #  potential errors.
            if ins_address not in common_objs.errored_instructions:
                common_objs.errored_instructions.append(ins_address)
                logging.trace(
                    'Branch target ('
                    + hex(branch_address)
                    + ') is not present is disassembled firmware '
                    + 'for call at '
                    + hex(ins_address)
                    + '. Adding to errored instructions.'
                )
            return disassembled_fw
        # If the target is data, then it is an invalid branch.
        if (disassembled_fw[branch_address]['is_data'] == True):
            if ins_address not in common_objs.errored_instructions:
                common_objs.errored_instructions.append(ins_address)
                logging.trace(
                    'Branch target ('
                    + hex(branch_address)
                    + ') is data '
                    + 'for call at '
                    + hex(ins_address)
                    + '. Adding to errored instructions.'
                )
            return disassembled_fw
        # If it was a BL to BL, it's unlikely to be correct.
        if ((disassembled_fw[branch_address]['insn'].id 
                in [ARM_INS_POP, ARM_INS_BL, ARM_INS_BLX, ARM_INS_BX]) 
                or ((disassembled_fw[branch_address]['insn'].id == ARM_INS_B) 
                    and (disassembled_fw[branch_address]['insn'].cc != ARM_CC_AL))):
            if insn.id == ARM_INS_BL:
                if ins_address not in common_objs.errored_instructions:
                    common_objs.errored_instructions.append(ins_address)
                    logging.trace(
                        'Branch target of BL ('
                        + hex(branch_address)
                        + ') is unlikely (BL/POP/etc) '
                        + 'for call at '
                        + hex(ins_address)
                        + '. Adding to errored instructions.'
                    )
                return disassembled_fw
        # Add back-links to disassembled firmware object. 
        if 'xref_from' not in disassembled_fw[branch_address]:
            disassembled_fw[branch_address]['xref_from'] = []
        if ins_address not in disassembled_fw[branch_address]['xref_from']:
            disassembled_fw[branch_address]['xref_from'].append(ins_address)
            
        return disassembled_fw
        
    def mark_last_instruction(self, disassembled_fw):
        last_good_instruction = common_objs.code_start_address
        for ins_address in disassembled_fw:
            if ins_address < common_objs.code_start_address:
                continue
            if utils.is_valid_code_address(ins_address) != True:
                continue
            if disassembled_fw[ins_address]['insn'].id == 0:
                continue
            if disassembled_fw[ins_address]['insn'].id == ARM_INS_NOP:
                continue
            if (disassembled_fw[ins_address]['insn'].id 
                    in [ARM_INS_MOV, ARM_INS_MOVT, ARM_INS_MOVW]):
                operands = disassembled_fw[ins_address]['insn'].operands
                if len(operands) == 2:
                    # Don't mark as data, because NOPs are sometimes used 
                    #  within functions.
                    if operands[0].value.reg == operands[1].value.reg:
                        continue
            disassembled_fw[ins_address]['last_insn_address'] = \
                last_good_instruction
            last_good_instruction = ins_address
        return disassembled_fw
        
    def test_arm_arch(self):
        """Test for ARM architecture version. We use this in function matching."""

        arch7m_ins = [ARM_INS_UDIV, ARM_INS_TBB, ARM_INS_TBH]
        all_addresses = list(common_objs.disassembled_firmware.keys())
        all_addresses.sort()
        ins_address = common_objs.code_start_address - 2
        while ins_address <= common_objs.code_end_address:
            ins_address += 2
            if utils.is_valid_code_address(ins_address) != True:
                continue
            if common_objs.disassembled_firmware[ins_address]['insn'].id == 0:
                continue
            if common_objs.disassembled_firmware[ins_address]['insn'].id in arch7m_ins:
                common_objs.arm_arch = consts.ARMv7M
        logging.debug('ARM architecture estimated to be ' + common_objs.arm_arch)
                
    def get_mem_access_pc_value(self, ins_address):
        curr_pc_value = ins_address + 4
        
        # When the PC is used as a base register for addressing operations 
        #  (i.e. adr/ldr/str/etc.) it is always the word-aligned value 
        #  that is used, even in Thumb state. 
        # So, whilst executing a load instruction at 0x159a, 
        #  the PC register will read as 0x159e, 
        #  but the base address of ldr...[pc] is Align(0x159e, 4), 
        #  i.e. 0x159c.
        # Ref: https://stackoverflow.com/a/29588678
        if ((curr_pc_value % 4) != 0):
            aligned_pc_value = curr_pc_value - (curr_pc_value % 4)
            curr_pc_value = aligned_pc_value
        return curr_pc_value
        
    def initialise_objects_for_trace(self, all_addresses, trace_start, 
            comp_reg, comp_val):
        strand_exec_inst = StrandExecution(all_addresses)
        # Initialise parameters.
        ## Initialise registers.
        init_regs = {}
        for reg in list(consts.REGISTERS.keys()):
            init_regs[reg] = None
            
        start_stack_pointer = int(common_objs.application_vector_table['initial_sp'])
        init_regs[ARM_REG_SP] = '{0:08x}'.format(start_stack_pointer)
        
        init_regs[ARM_REG_PC] = \
            '{0:08x}'.format(strand_exec_inst.get_pc_value(trace_start))
            
        hex_comp_value = utils.convert_type(np.uint8(comp_val), 'hex')
        init_regs[comp_reg] = hex_comp_value.zfill(8)
        
        ## Initialise path.
        current_path = hex(trace_start)
        ## Initialise condition flags.
        condition_flags = strand_exec_inst.initialise_condition_flags()
        
        return (strand_exec_inst, init_regs, condition_flags, current_path)