import hashlib

class NexusVM:
    """Actor 模型虚拟机"""
    @staticmethod
    def execute(code, msg):
        return f"VM执行: {msg}"

def deploy_address(bytecode: str) -> str:
    return "0x" + hashlib.sha3_256(bytecode.encode()).hexdigest()[:40]