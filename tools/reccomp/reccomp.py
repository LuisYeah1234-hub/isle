#!/usr/bin/env python3

from capstone import *
import difflib
import struct
import subprocess
import os
import sys

def print_usage():
  print('Usage: %s [options] <original-binary> <recompiled-binary> <recompiled-pdb> <decomp-dir>\n' % sys.argv[0])
  print('\t-v, --verbose <offset>\t\t\tPrint assembly diff for specific function (original file\'s offset)')
  print('\t-h, --html <output-file>\t\t\tGenerate searchable HTML summary of status and diffs')
  sys.exit(1)

positional_args = []
verbose = None
skip = False
html = None

for i, arg in enumerate(sys.argv):
  if skip:
    skip = False
    continue

  if arg.startswith('-'):
    # A flag rather than a positional arg
    flag = arg[1:]

    if flag == 'v' or flag == '-verbose':
      verbose = int(sys.argv[i + 1], 16)
      skip = True
    elif flag == 'h' or flag == '-html':
      html = sys.argv[i + 1]
      skip = True
    else:
      print('Unknown flag: %s' % arg)
      print_usage()
  else:
    positional_args.append(arg)

if len(positional_args) != 5:
  print_usage()

original = positional_args[1]
if not os.path.isfile(original):
  print('Invalid input: Original binary does not exist')
  sys.exit(1)

recomp = positional_args[2]
if not os.path.isfile(recomp):
  print('Invalid input: Recompiled binary does not exist')
  sys.exit(1)

syms = positional_args[3]
if not os.path.isfile(syms):
  print('Invalid input: Symbols PDB does not exist')
  sys.exit(1)

source = positional_args[4]
if not os.path.isdir(source):
  print('Invalid input: Source directory does not exist')
  sys.exit(1)

# Declare a class that can automatically convert virtual executable addresses
# to file addresses
class Bin:
  def __init__(self, filename):
    self.file = open(filename, 'rb')

    #HACK: Strictly, we should be parsing the header, but we know where
    #      everything is in these two files so we just jump straight there

    # Read ImageBase
    self.file.seek(0xB4)
    self.imagebase = struct.unpack('i', self.file.read(4))[0]

    # Read .text VirtualAddress
    self.file.seek(0x184)
    self.textvirt = struct.unpack('i', self.file.read(4))[0]

    # Read .text PointerToRawData
    self.file.seek(0x18C)
    self.textraw = struct.unpack('i', self.file.read(4))[0]

  def __del__(self):
    if self.file:
      self.file.close()

  def get_addr(self, virt):
    return virt - self.imagebase - self.textvirt + self.textraw

  def read(self, offset, size):
    self.file.seek(self.get_addr(offset))
    return self.file.read(size)

class RecompiledInfo:
  addr = None
  size = None
  name = None

def get_wine_path(fn):
  return subprocess.check_output(['winepath', '-w', fn]).decode('utf-8').strip()

def get_unix_path(fn):
  return subprocess.check_output(['winepath', fn]).decode('utf-8').strip()

def get_file_in_script_dir(fn):
  return os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), fn)

# Declare a class that parses the output of cvdump for fast access later
class SymInfo:
  funcs = {}
  lines = {}

  def __init__(self, pdb, file):
    call = [get_file_in_script_dir('cvdump.exe'), '-l', '-s']

    if os.name != 'nt':
      # Run cvdump through wine and convert path to Windows-friendly wine path
      call.insert(0, 'wine')
      call.append(get_wine_path(pdb))
    else:
      call.append(pdb)

    print('Parsing %s...' % pdb)

    line_dump = subprocess.check_output(call).decode('utf-8').split('\r\n')

    current_section = None

    for i, line in enumerate(line_dump):
      if line.startswith('***'):
        current_section = line[4:]

      if current_section == 'SYMBOLS' and 'S_GPROC32' in line:
        addr = int(line[26:34], 16)

        info = RecompiledInfo()
        info.addr = addr + recompfile.imagebase + recompfile.textvirt
        info.size = int(line[41:49], 16)
        info.name = line[77:]

        self.funcs[addr] = info
      elif current_section == 'LINES' and line.startswith('  ') and not line.startswith('   '):
        sourcepath = line.split()[0]

        if os.name != 'nt':
          # Convert filename to Unix path for file compare
          sourcepath = get_unix_path(sourcepath)

        if sourcepath not in self.lines:
          self.lines[sourcepath] = {}

        j = i + 2
        while True:
          ll = line_dump[j].split()
          if len(ll) == 0:
            break

          k = 0
          while k < len(ll):
            linenum = int(ll[k + 0])
            address = int(ll[k + 1], 16)
            if linenum not in self.lines[sourcepath]:
              self.lines[sourcepath][linenum] = address
            k += 2

          j += 1

  def get_recompiled_address(self, filename, line):
    addr = None
    found = False

    #print('Looking for ' + filename + ' line ' + str(line))

    for fn in self.lines:
      # Sometimes a PDB is compiled with a relative path while we always have
      # an absolute path. Therefore we must
      if os.path.samefile(fn, filename):
        filename = fn
        break

    if filename in self.lines and line in self.lines[fn]:
      addr = self.lines[fn][line]

      if addr in self.funcs:
        return self.funcs[addr]
      else:
        print('Failed to find function symbol with address: %s' % hex(addr))
    else:
      print('Failed to find function symbol with filename and line: %s:%s' % (filename, str(line)))

origfile = Bin(original)
recompfile = Bin(recomp)
syminfo = SymInfo(syms, recompfile)

print()

md = Cs(CS_ARCH_X86, CS_MODE_32)

def sanitize(file, mnemonic, op_str):
  offsetplaceholder = '<OFFSET>'

  if mnemonic == 'call' or mnemonic == 'jmp':
    # Filter out "calls" because the offsets we're not currently trying to
    # match offsets. As long as there's a call in the right place, it's
    # probably accurate.
    op_str = offsetplaceholder
  else:
    def filter_out_ptr(ptype, op_str):
      try:
        ptrstr = ptype + ' ptr ['
        start = op_str.index(ptrstr) + len(ptrstr)
        end = op_str.index(']', start)

        # This will throw ValueError if not hex
        inttest = int(op_str[start:end], 16)

        return op_str[0:start] + offsetplaceholder + op_str[end:]
      except ValueError:
        return op_str

    # Filter out dword ptrs where the pointer is to an offset
    op_str = filter_out_ptr('dword', op_str)
    op_str = filter_out_ptr('word', op_str)
    op_str = filter_out_ptr('byte', op_str)

    # Use heuristics to filter out any args that look like offsets
    words = op_str.split(' ')
    for i, word in enumerate(words):
      try:
        inttest = int(word, 16)
        if inttest >= file.imagebase + file.textvirt:
          words[i] = offsetplaceholder
      except ValueError:
        pass
    op_str = ' '.join(words)

  return mnemonic, op_str

def parse_asm(file, addr, size):
  asm = []
  data = file.read(addr, size)
  for i in md.disasm(data, 0):
    # Use heuristics to disregard some differences that aren't representative
    # of the accuracy of a function (e.g. global offsets)
    mnemonic, op_str = sanitize(file, i.mnemonic, i.op_str)
    if op_str is None:
      asm.append(mnemonic)
    else:
      asm.append("%s %s" % (mnemonic, op_str))
  return asm

function_count = 0
total_accuracy = 0
htmlinsert = []

for subdir, dirs, files in os.walk(source):
  for file in files:
    srcfilename = os.path.join(os.path.abspath(subdir), file)
    srcfile = open(srcfilename, 'r')
    line_no = 0

    while True:
      try:
        line = srcfile.readline()
        line_no += 1

        if not line:
          break

        if line.startswith('// OFFSET:'):
          par = line[10:].strip().split()
          module = par[0]
          addr = int(par[1], 16)

          find_open_bracket = line
          while '{' not in find_open_bracket:
            find_open_bracket = srcfile.readline()
            line_no += 1

          recinfo = syminfo.get_recompiled_address(srcfilename, line_no)
          if not recinfo:
            continue

          origasm = parse_asm(origfile, addr, recinfo.size)
          recompasm = parse_asm(recompfile, recinfo.addr, recinfo.size)

          diff = difflib.SequenceMatcher(None, origasm, recompasm)
          ratio = diff.ratio()
          print('  %s (%s / %s) is %.2f%% similar to the original' % (recinfo.name, hex(addr), hex(recinfo.addr), ratio * 100))

          function_count += 1
          total_accuracy += ratio

          if verbose == addr or html:
            udiff = difflib.unified_diff(origasm, recompasm)

            if verbose == addr:
              for line in udiff:
                print(line)
              print()
              print()

            if html:
              htmlinsert.append('{address: "%s", name: "%s", matching: %s, diff: "%s"}' % (hex(addr), recinfo.name, str(ratio), '\\n'.join(udiff).replace('"', '\\"').replace('\n', '\\n')))

      except UnicodeDecodeError:
        break

def gen_html(html, data):
  templatefile = open(get_file_in_script_dir('template.html'), 'r')
  if not templatefile:
    print('Failed to find HTML template file, can\'t generate HTML summary')
    return

  templatedata = templatefile.read()
  templatefile.close()

  templatedata = templatedata.replace('/* INSERT DATA HERE */', ','.join(data), 1)

  htmlfile = open(html, 'w')
  if not htmlfile:
    print('Failed to write to HTML file %s' % html)
    return

  htmlfile.write(templatedata)
  htmlfile.close()

if html:
  gen_html(html, htmlinsert)

if function_count > 0:
  print('\nTotal accuracy %.2f%% across %i functions' % (total_accuracy / function_count * 100, function_count))