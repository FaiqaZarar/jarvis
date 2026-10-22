# DMG/app icon sources

`icon.svg` and `dmg_background.svg` are the editable sources for
`../icon.icns` and `../dmg_background.png`. Regenerate after editing
either file (requires `brew install librsvg`, for alpha-correct SVG
rasterization - `qlmanage`/`sips` flatten transparency to opaque
white, which shows up as a white card behind the app icon in Finder):

```bash
cd packaging/assets

rsvg-convert -w 1024 -h 1024 -o /tmp/icon_master.png src/icon.svg
rm -rf icon.iconset && mkdir icon.iconset
for sz in 16 32 128 256 512; do
  sips -z $sz $sz /tmp/icon_master.png --out icon.iconset/icon_${sz}x${sz}.png
  d=$((sz*2))
  sips -z $d $d /tmp/icon_master.png --out icon.iconset/icon_${sz}x${sz}@2x.png
done
iconutil -c icns icon.iconset -o icon.icns
rm -rf icon.iconset

rsvg-convert -w 1320 -h 800 -o dmg_background.png src/dmg_background.svg
```

`dmg_background.png` is rendered at 1320x800 (2x) for a 660x400 Finder
window - see the `create-dmg` invocation in
`.github/workflows/build.yml`'s `build-macos` job for the icon
positions it was drawn to match.
