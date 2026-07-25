# Brand assets

This directory stores public-facing Avalanche brand assets used by docs,
READMEs, package pages, and social previews.

## Files

- `avalanche-logo-3d.png` — transparent 3D lockup with diamond mark and
  `avalanche.run` wordmark.
- `avalanche-diamond-3d-1024.png` — transparent 1024×1024 standalone 3D
  diamond mark.
- `source/avalanche-diamond-threejs-spin-with-wordmark.html` — interactive
  Three.js source for the animated lockup.
- `source/avalanche-diamond-threejs-spin-with-wordmark-export-start.html` —
  frozen-start Three.js source used for static PNG export.

## Build

Run from the repository root:

```bash
make brand
```

The export requires Google Chrome and ImageMagick 7's `magick` command. On
macOS, install ImageMagick with:

```bash
brew install imagemagick
```

Set `CHROME=/path/to/chrome` when Chrome is not installed at the default macOS
application path, or `MAGICK=/path/to/magick` when `magick` is not on `PATH`.

The command rebuilds the compiled PNGs from the checked-in Three.js source HTML:

- `avalanche-logo-3d.png`
- `avalanche-diamond-3d-1024.png`

## Source

The current logo sources and compiled PNG artifacts were copied from the Icicle
logo workspace. Keep heavier source/export experiments out of this repo unless
this becomes the canonical brand-kit repository.
