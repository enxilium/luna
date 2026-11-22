"""Install and supervise Luna's free local memory stack (the app's setup does the same; see src/install/memory.py)."""

from src.install import InstallError
from src.install.memory import install


class _Console:
    """Prints the installer's steps and download progress the way the setup page shows them."""

    def __init__(self):
        self.last = None

    def report(self, step=None, done=None, total=None):
        if step and step != self.last:
            self.last = step
            print(f"{step}…", flush=True)
        if done is not None and total:
            print(f"\r  {done / 1e9:.1f} of {total / 1e9:.1f} GB", end="", flush=True)


def main():
    try:
        install(_Console())
    except InstallError as error:
        raise SystemExit(f"\nMemory setup stopped: {error.reason}. Logs are in Luna's memory folder.") from None
    print("\nLuna's local memory is ready.")


if __name__ == "__main__":
    main()
