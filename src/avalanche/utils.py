from urllib.parse import urlparse, urlunparse


def urljoin(base, relative):
    base_parts = urlparse(base)
    relative_parts = urlparse(relative)

    # Replace the path in the base URL with the joined path
    new_path = base_parts.path.rstrip("/") + "/" + relative_parts.path.lstrip("/")

    # Create a new URL with the updated path
    new_url = urlunparse(
        (
            base_parts.scheme,
            base_parts.netloc,
            new_path,
            relative_parts.params,
            relative_parts.query,
            relative_parts.fragment,
        )
    )

    return new_url
