import logging
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import List, Optional, Sequence


class ProtonVpnManager:
    """Lightweight Proton VPN CLI integration for optional routing/rotation."""

    def __init__(
        self,
        *,
        cli_path: str = "protonvpn",
        server: str = "",
        country: str = "",
        require_connected: bool = False,
        min_session_minutes: int = 10,
        reconnect_on_network_error: bool = True,
        rotate_on_captcha: bool = True,
    ) -> None:
        self._cli_args = shlex.split(cli_path or "protonvpn")
        if not self._cli_args:
            self._cli_args = ["protonvpn"]

        self.server = server.strip()
        self.country = country.strip()
        self.require_connected = require_connected
        self.min_session_minutes = max(0, min_session_minutes)
        self.reconnect_on_network_error = reconnect_on_network_error
        self.rotate_on_captcha = rotate_on_captcha

        self._last_connect_time: Optional[datetime] = None
        self._last_status: Optional[bool] = None
        self._logged_missing = False

    @property
    def available(self) -> bool:
        cmd = self._cli_args[0]
        return shutil.which(cmd) is not None

    def _ensure_available(self) -> bool:
        if self.available:
            return True
        if not self._logged_missing:
            logging.info(
                "Proton VPN CLI not found at '%s'; skipping VPN automation",
                self._cli_args[0],
            )
            self._logged_missing = True
        return False

    def _run_command(self, args: Sequence[str], *, timeout: int = 60) -> Optional[subprocess.CompletedProcess[str]]:
        cmd: List[str] = list(self._cli_args) + list(args)
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            logging.debug("Proton VPN command failed (%s): %s", " ".join(cmd), exc)
            return None

    def status(self) -> Optional[bool]:
        """Return connection status (True/False) or None if unknown."""
        if not self._ensure_available():
            return None

        for args in (["status"], ["s"]):
            result = self._run_command(args, timeout=15)
            if result is None:
                continue
            output = (result.stdout or "") + (result.stderr or "")
            lower = output.lower()

            if "connected" in lower and "disconnected" not in lower:
                self._last_status = True
                return True
            if "disconnected" in lower or "not connected" in lower:
                self._last_status = False
                return False

        return self._last_status

    def _build_connect_variants(self) -> List[List[str]]:
        def _variant(verb: str) -> List[str]:
            args: List[str] = [verb]
            if self.server:
                args.append(self.server)
            elif self.country:
                args.extend(["--cc", self.country])
            else:
                args.append("--fastest")
            return args

        return [_variant("connect"), _variant("c")]

    def connect(self, *, reason: str = "") -> bool:
        if not self._ensure_available():
            return False

        for variant in self._build_connect_variants():
            result = self._run_command(variant, timeout=90)
            if result is None:
                continue

            success = result.returncode == 0
            if success:
                self._last_connect_time = datetime.now()
                self._last_status = True
                logging.info(
                    "Proton VPN connected via '%s'%s",
                    " ".join(variant),
                    f" (reason: {reason})" if reason else "",
                )
                return True

            output = (result.stderr or result.stdout or "").strip()
            logging.debug(
                "Proton VPN connect attempt failed (%s): %s",
                " ".join(variant),
                output,
            )

        logging.warning(
            "Proton VPN connection attempt failed%s",
            f" ({reason})" if reason else "",
        )
        self._last_status = False
        return False

    def ensure_connected(self, *, reason: str) -> bool:
        """Ensure a VPN session is active, respecting require_connected."""
        if not self._ensure_available():
            return not self.require_connected

        status = self.status()
        if status:
            return True

        if status is None:
            logging.debug("Proton VPN status unknown; attempting to connect")
        connected = self.connect(reason=reason)
        if not connected and self.require_connected:
            logging.warning(
                "Proton VPN is required but connection failed (%s)", reason
            )
            return False
        return connected or not self.require_connected

    def rotate(self, *, reason: str) -> bool:
        """Reconnect to obtain a fresh exit IP."""
        if not self._ensure_available():
            return False

        if self.min_session_minutes and self._last_connect_time:
            elapsed = datetime.now() - self._last_connect_time
            if elapsed < timedelta(minutes=self.min_session_minutes):
                logging.info(
                    "Proton VPN session age %.1fm < %dm; skipping rotation (%s)",
                    elapsed.total_seconds() / 60,
                    self.min_session_minutes,
                    reason,
                )
                return False

        self._run_command(["disconnect"], timeout=30)
        return self.connect(reason=reason)

    def handle_network_issue(self, *, reason: str) -> None:
        """Optional reconnect on network failures."""
        if not self.reconnect_on_network_error:
            return
        self.rotate(reason=reason)

    def handle_captcha_block(self) -> None:
        """Optionally rotate after CAPTCHA blocks to change exit IP."""
        if not self.rotate_on_captcha:
            return
        self.rotate(reason="captcha detected")
