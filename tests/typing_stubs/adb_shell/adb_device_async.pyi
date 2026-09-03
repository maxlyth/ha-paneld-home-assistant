from collections.abc import AsyncIterator, Callable, Sequence

from .auth.sign_pythonrsa import PythonRSASigner
from .transport.tcp_transport_async import TcpTransportAsync

class AdbDeviceAsync:
    _maxdata: int

    def __init__(
        self,
        transport: TcpTransportAsync,
        default_transport_timeout_s: float | None = ...,
        banner: str | None = ...,
    ) -> None: ...
    async def connect(
        self,
        rsa_keys: Sequence[PythonRSASigner] | None = ...,
        transport_timeout_s: float | None = ...,
        auth_timeout_s: float = ...,
        read_timeout_s: float = ...,
        auth_callback: Callable[[AdbDeviceAsync], None] | None = ...,
    ) -> bool: ...
    def streaming_shell(
        self,
        command: str,
        transport_timeout_s: float | None = ...,
        read_timeout_s: float = ...,
        timeout_s: float | None = ...,
        decode: bool = ...,
    ) -> AsyncIterator[bytes]: ...
    async def push(
        self,
        local_path: str,
        device_path: str,
        st_mode: int = ...,
        mtime: int = ...,
        progress_callback: Callable[[str, int, int], None] | None = ...,
        transport_timeout_s: float | None = ...,
        read_timeout_s: float = ...,
    ) -> None: ...
    async def close(self) -> None: ...
