class NexLangCompiler:
    def __init__(self):
        self.bytecode = []
        self.storage_slots = {}
        self.next_slot = 0
        self.labels = {}
        self.pending_jumps = []

    def compile(self, source: str) -> list:
        lines = source.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith('//'):
                i += 1
                continue

            if line.startswith('on_') or line.startswith('query '):
                func_name = line.split('(')[0].split(' ')[-1]
                self.labels[func_name] = len(self.bytecode)
                if '{' not in line:
                    i += 1
                    continue
                i += 1
                body_lines = []
                depth = 1
                while depth > 0 and i < len(lines):
                    l = lines[i]
                    if '{' in l: depth += l.count('{')
                    if '}' in l: depth -= l.count('}')
                    if depth > 0: body_lines.append(l.strip())
                    i += 1
                self._compile_body(body_lines)

            elif line.startswith('let '):
                var_name = line.split(' ')[1]
                if '=' in line:
                    expr = line.split('=')[1].strip().rstrip(';')
                    self._compile_expression(expr)
                    self._allocate_slot(var_name)
                    self._emit(0x02, self.storage_slots[var_name])
                i += 1

            elif line.startswith('send('):
                args = line[5:-2].split(',')
                self._compile_expression(args[1].strip())
                self._compile_expression(args[0].strip())
                self._emit(0x04)
                i += 1

            elif line.startswith('return '):
                expr = line[7:].rstrip(';')
                self._compile_expression(expr)
                self._emit(0x05)
                i += 1

            else:
                i += 1

        return self.bytecode

    def _compile_body(self, lines):
        i = 0
        while i < len(lines):
            line = lines[i]
            if 'let ' in line:
                i += 1
            elif line.startswith('send('):
                args = line[5:-2].split(',')
                self._compile_expression(args[1].strip())
                self._compile_expression(args[0].strip())
                self._emit(0x04)
                i += 1
            elif line.startswith('return '):
                expr = line[7:].rstrip(';')
                self._compile_expression(expr)
                self._emit(0x05)
                i += 1
            else:
                i += 1

    def _compile_expression(self, expr):
        expr = expr.strip()
        if expr.isdigit():
            self._emit(0x01, int(expr))
        elif expr in self.storage_slots:
            self._emit(0x01, self.storage_slots[expr])
        elif '*' in expr:
            left, right = expr.split('*')
            self._compile_expression(left.strip())
            self._compile_expression(right.strip())
            self._emit(0x03)
        elif '/' in expr:
            left, right = expr.split('/')
            self._compile_expression(left.strip())
            self._compile_expression(right.strip())
            self._emit(0x03)
        elif '-' in expr:
            left, right = expr.split('-')
            self._compile_expression(left.strip())
            self._compile_expression(right.strip())
            self._emit(0x03)

    def _allocate_slot(self, name):
        if name not in self.storage_slots:
            self.storage_slots[name] = self.next_slot
            self.next_slot += 1

    def _emit(self, opcode, operand=None):
        self.bytecode.append(opcode)
        if operand is not None:
            self.bytecode.append(operand)