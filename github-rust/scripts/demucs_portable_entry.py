"""PyInstaller entry point for portable Demucs.

This includes a Python runtime *inside* the packaged binary directory; the target
Windows PC does not need Python installed. This is not Rust-native inference.
"""
import multiprocessing


def main():
    multiprocessing.freeze_support()
    from demucs.separate import main as demucs_main
    demucs_main()


if __name__ == "__main__":
    main()
