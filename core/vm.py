"""NexusVM：NexLang 字节码的确定性栈式执行器。

操作码与 nexlang_compiler 对齐：
    0x01 PUSH  压入常量/槽值
    0x02 STORE 弹出值写入存储槽
    0x03 MUL   栈顶两数相乘
    0x04 SEND  弹出 [to, amount]，记录链上转账事件
    0x05 RET   弹出返回值并停止
    0x0A ADD   栈顶两数相加
    0x0B SUB   栈顶两数相减
    0x0C DIV   栈顶两数相除（除零返回 0）
"""
import hashlib

PUSH, STORE, MUL, SEND, RET, LOAD = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
ADD, SUB, DIV = 0x0A, 0x0B, 0x0C


def deploy_address(bytecode: str) -> str:
    return "0x" + hashlib.sha3_256(bytecode.encode()).hexdigest()[:40]


class NexusVM:
    def __init__(self, bytecode):
        self.code = bytecode if isinstance(bytecode, list) else []
        self.pc = 0
        self.stack = []
        self.storage = {}
        self.events = []
        self.result = 0
        self._halted = False

    def _read(self):
        if self.pc >= len(self.code):
            raise StopIteration("程序结束")
        op = self.code[self.pc]
        self.pc += 1
        return op

    def _operand(self):
        if self.pc >= len(self.code):
            raise StopIteration("缺少操作数")
        val = self.code[self.pc]
        self.pc += 1
        return int(val) if not isinstance(val, bool) else int(val)

    def _binary(self, fn):
        if len(self.stack) < 2:
            return
        b = self.stack.pop()
        a = self.stack.pop()
        try:
            self.stack.append(fn(a, b))
        except ZeroDivisionError:
            self.stack.append(0)

    def run(self, msg="", amount=0, sender="", storage=None):
        """执行字节码；storage 为合约持久化槽（dict），执行后原地更新。"""
        self.storage = storage if isinstance(storage, dict) else {}
        self.pc = 0
        self.stack = []
        self.events = []
        self._halted = False
        steps = 0
        max_steps = 100000
        try:
            while not self._halted and steps < max_steps:
                steps += 1
                op = self._read()
                if op == PUSH:
                    self.stack.append(self._operand())
                elif op == STORE:
                    if self.stack:
                        slot = self._operand()
                        self.storage[slot] = self.stack.pop()
                elif op == LOAD:
                    self.stack.append(self.storage.get(self._operand(), 0))
                elif op == ADD:
                    self._binary(lambda a, b: a + b)
                elif op == SUB:
                    self._binary(lambda a, b: a - b)
                elif op == MUL:
                    self._binary(lambda a, b: a * b)
                elif op == DIV:
                    self._binary(lambda a, b: a / b)
                elif op == SEND:
                    if len(self.stack) >= 2:
                        to = self.stack.pop()
                        amt = self.stack.pop()
                        self.events.append(("send", str(to), float(amt)))
                elif op == RET:
                    self.result = self.stack.pop() if self.stack else 0
                    self._halted = True
                else:
                    continue  # 未知操作码：跳过，保证确定性
        except StopIteration:
            pass
        return {
            "result": self.result,
            "storage": dict(self.storage),
            "events": list(self.events),
            "steps": steps,
        }

    @staticmethod
    def execute(code, msg):
        """兼容旧接口：直接运行并返回结果字符串。"""
        try:
            r = NexusVM(code).run(msg=msg)
            return f"VM执行: {msg} → {r['result']}"
        except Exception as e:
            return f"VM执行: {msg} (错误 {e})"