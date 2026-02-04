import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent

import pytest

from inspect_kathara.sandbox import (
    generate_compose_for_inspect,
)


@contextmanager
def compose_stack(compose_file: Path, project_dir: Path):
    if not compose_file.exists():
        raise FileNotFoundError(f"compose file not found: {compose_file}")

    base = ["docker", "compose"]
    base += ["-f", str(compose_file)]

    try:
        subprocess.run(
            base + ["up", "-d"],
            check=True,
            capture_output=True,
            timeout=120,
            cwd=str(project_dir),
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        pytest.skip(f"docker compose up failed or docker not available: {e}")

    try:
        yield
    finally:
        subprocess.run(
            base + ["down", "-t", "2"],
            capture_output=True,
            timeout=30,
            cwd=str(project_dir),
        )


class TestMachineStartup:
    """Tests after machine startup."""

    def test_machine_no_default_ips(self):
        """After compose is up, check all machines have no IPv4 in default network."""

        # Define util functions
        # Match "inet 10.0.1.1/24" or "inet 172.17.0.2/16" in `ip -4 addr show` output
        INET4_RE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/\d+")

        def _get_ipv4_addresses_from_container(container_id: str) -> list[str]:
            """Run `ip -4 addr show` in container and return list of IPv4 addresses (no CIDR)."""
            result = subprocess.run(
                ["docker", "exec", container_id, "ip", "-4", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            result.check_returncode()
            return INET4_RE.findall(result.stdout)

        def _machines_no_default_ips(
            compose_file: Path, project_dir: Path, default_prefix: str = "172."
        ) -> tuple[bool, list[str]]:
            """
            After compose is up, check all containers have no IPv4 in default network.
            Returns (all_ok, list of violation messages).
            """
            violations: list[str] = []
            cmd = ["docker", "compose", "-f", str(compose_file)]
            if project_dir is not None:
                cmd += ["--project-directory", str(project_dir)]
            try:
                ps = subprocess.run(
                    cmd + ["ps", "-q"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=project_dir or compose_file.parent,
                )
                ps.check_returncode()
                container_ids = [c.strip() for c in ps.stdout.strip().splitlines() if c.strip()]
            except subprocess.CalledProcessError as e:
                return False, [f"compose ps failed: {e.stderr or e}"]
            except FileNotFoundError:
                return False, ["docker or docker compose not available"]

            for cid in container_ids:
                try:
                    addrs = _get_ipv4_addresses_from_container(cid)
                except subprocess.CalledProcessError:
                    violations.append(f"container {cid[:12]}: failed to get ip addr")
                    continue
                for addr in addrs:
                    if addr.startswith(default_prefix):
                        violations.append(f"container {cid[:12]}: has {default_prefix} address {addr}")

            return len(violations) == 0, violations

        lab_conf = dedent("""\
        pc1[0]="lan1"
        router[0]="lan1"
        pc1[image]="kathara/base"
        router[image]="kathara/frr"
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            lab_path = Path(tmpdir)
            (lab_path / "topology").mkdir(parents=True, exist_ok=True)
            (lab_path / "topology" / "lab.conf").write_text(lab_conf)
            # Create startup file
            pc1_startup = "ip addr add 10.0.1.1/24 dev eth0"
            (lab_path / "topology" / "pc1.startup").write_text(pc1_startup)
            router_startup = "ip addr add 10.0.1.2/24 dev eth0"
            (lab_path / "topology" / "router.startup").write_text(router_startup)

            compose_yaml = generate_compose_for_inspect(lab_path)
            compose_file = lab_path / "compose.yaml"
            compose_file.write_text(compose_yaml)

            with compose_stack(compose_file=compose_file, project_dir=lab_path):
                time.sleep(5)
                ok, violations = _machines_no_default_ips(compose_file=compose_file, project_dir=lab_path)
                assert ok, "Found default network addresses: " + "; ".join(violations)

    def test_machine_has_conf_files(self):
        """After compose is up, check all machines have config files."""

        def _machines_has_conf_files(
            compose_file: Path, project_dir: Path, target_host: str, target_file: str
        ) -> tuple[bool, list[str]]:
            """After compose is up, check target machine has config files."""

            violations: list[str] = []
            # Get all container names
            cmd = ["docker", "compose", "-f", str(compose_file)]
            if project_dir is not None:
                cmd += ["--project-directory", str(project_dir)]
            try:
                ps = subprocess.run(
                    cmd + ["ps", "-q"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=project_dir or compose_file.parent,
                )
                ps.check_returncode()
                container_ids = [c.strip() for c in ps.stdout.strip().splitlines() if c.strip()]
                container_names = []
                for cid in container_ids:
                    inspect = subprocess.run(
                        ["docker", "inspect", cid, "--format", "{{.Config.Hostname}}"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    inspect.check_returncode()
                    container_names.append(inspect.stdout.strip().split()[0])
            except subprocess.CalledProcessError as e:
                return False, [f"compose ps failed: {e.stderr or e}"]
            except FileNotFoundError:
                return False, ["docker or docker compose not available"]

            for cname in container_names:
                if cname != target_host:
                    continue
                if not (project_dir / "topology" / cname / target_file).exists():
                    violations.append(f"container {cname}: config file not found")

            return len(violations) == 0, violations

        lab_conf = dedent("""\
        pc1[0]="lan1"
        router[0]="lan1"
        pc1[image]="kathara/base"
        router[image]="kathara/frr"
        """)
        config_file = "pc1.conf"
        config_text = "Hello, World!"

        with tempfile.TemporaryDirectory() as tmpdir:
            lab_path = Path(tmpdir)
            (lab_path / "topology").mkdir(parents=True, exist_ok=True)
            (lab_path / "topology" / "lab.conf").write_text(lab_conf)
            # Create config directory and file
            (lab_path / "topology" / "pc1").mkdir(parents=True, exist_ok=True)
            (lab_path / "topology" / "pc1" / config_file).write_text(config_text)

            compose_yaml = generate_compose_for_inspect(lab_path)
            compose_file = lab_path / "compose.yaml"
            compose_file.write_text(compose_yaml)

            with compose_stack(compose_file=compose_file, project_dir=lab_path):
                time.sleep(5)
                ok, violations = _machines_has_conf_files(
                    compose_file=compose_file, project_dir=lab_path, target_host="pc1", target_file=config_file
                )
                assert ok, "Config file not found: " + "; ".join(violations)
