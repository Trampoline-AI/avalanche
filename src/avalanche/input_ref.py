class InputRef:
    __slots__ = ("path",)

    def __init__(self, path: tuple[str, ...]) -> None:
        object.__setattr__(self, "path", tuple(path))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("InputRef is immutable")

    def __getattr__(self, name: str) -> "InputRef":
        if name.startswith("_"):
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        return InputRef(self.path + (name,))

    def __reduce__(self):
        """Reconstruct immutable selectors across executor serialization."""
        return type(self), (self.path,)

    def __repr__(self) -> str:
        return "ava.input" + "".join(f".{part}" for part in self.path)


INPUT = InputRef(())
