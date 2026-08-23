#!/bin/sh
# Capture a sanitized Pi boot drive to a compressed image. Runs ON macOS, with the drive
# moved over from the Pi.
#
#   deploy/pi/capture-image.sh                       # list candidate disks and stop
#   deploy/pi/capture-image.sh disk4                 # capture to sigen-pi-<date>.img.xz
#   deploy/pi/capture-image.sh disk4 my-image.img.xz
#
# ------------------------------------------------------------------------------------------
# THIS IS THE ONE SCRIPT IN THIS REPOSITORY THAT CAN DESTROY SOMETHING.
#
# It only ever READS the drive -- there is no `of=` pointing at a disk anywhere below. But it
# takes a raw device identifier from a human, and the difference between the right one and
# your own boot disk is a single character. So it refuses anything that is not external and
# removable, prints what it is about to read, and makes you type the identifier a second time.
# ------------------------------------------------------------------------------------------
#
# WHY A WHOLE-DRIVE IMAGE, AND WHAT IT COSTS. Shrinking an ext4 partition needs resize2fs and
# a loop device, neither of which macOS has. Sanitize zero-fills the free space instead, so
# xz compresses the unused part to almost nothing and the FILE is roughly the used size -- but
# the image still describes a drive of the original size, so:
#
#   *** it can only be written to a drive at least as large as the one it came from. ***
#
# Keep a matched pair of thumb drives. Nominal capacities lie -- two "32 GB" drives from
# different makers differ by megabytes -- so verify the target is bigger, or capture from the
# smaller of the pair. If that becomes a real problem, shrink the .img with pishrink inside a
# privileged Linux container rather than teaching macOS about ext4.
set -e

[ "$(uname)" = Darwin ] || {
  echo "this uses diskutil, so it expects macOS. On Linux: lsblk, then dd | xz." >&2
  exit 1; }
command -v xz >/dev/null 2>&1 || {
  echo "no xz. brew install xz" >&2; exit 1; }

DISK=$1
OUT=$2

is_external() {   # is_external <diskutil info output>
  printf '%s' "$1" | grep -qE '^ *(Device Location: *External|Internal: *No)$'
}

list_disks() {
  echo "External disks:"
  echo
  for d in $(diskutil list | sed -n 's/^\/dev\/\(disk[0-9]*\) .*/\1/p'); do
    info=$(diskutil info "$d" 2>/dev/null) || continue
    is_external "$info" || continue
    printf '  %-8s %-10s %s\n' "$d" \
      "$(printf '%s' "$info" | sed -n 's/.*Disk Size: *\([^(]*\).*/\1/p' | head -1)" \
      "$(printf '%s' "$info" | sed -n 's/^ *Device \/ Media Name: *//p' | head -1)"
  done
  echo
  echo "Then: $0 <disk-identifier> [output.img.xz]"
}

if [ -z "$DISK" ]; then
  list_disks
  exit 1
fi

# Accept "disk4" or "/dev/disk4"; normalise to the bare identifier.
DISK=$(printf '%s' "$DISK" | sed 's|^/dev/||; s|^r||')
case "$DISK" in
  disk[0-9]*) ;;
  *) echo "'$DISK' is not a disk identifier like disk4" >&2; exit 1 ;;
esac

INFO=$(diskutil info "$DISK") || { echo "no such disk: $DISK" >&2; exit 1; }

# --- the refusals ------------------------------------------------------------------------
# Keyed on EXTERNAL rather than on "Removable Media", which sounds like the right test and is
# not: diskutil reports a USB SSD as Fixed, so a removable-only check would refuse the very
# upgrade this drive is most likely to get. External, not virtual, and not the disk you booted
# from is the envelope that holds for both.
fail() { echo "REFUSING: $1" >&2; exit 1; }

is_external "$INFO" \
  || fail "$DISK is not an external disk."
printf '%s' "$INFO" | grep -q "Virtual: *Yes" \
  && fail "$DISK is a virtual disk (a disk image or APFS container), not a drive."
[ "$(diskutil info / | sed -n 's/^ *Part of Whole: *//p')" = "$DISK" ] \
  && fail "$DISK is the disk your root filesystem is on."

SIZE=$(printf '%s' "$INFO" | sed -n 's/.*Disk Size: *\([^(]*\).*/\1/p' | head -1)
NAME=$(printf '%s' "$INFO" | sed -n 's/^ *Device \/ Media Name: *//p' | head -1)

# Raspberry Pi OS names its boot partition "bootfs"; its absence is the cheapest sign that
# this is the wrong drive, or that it was never a Pi boot drive at all.
if ! diskutil list "$DISK" | grep -qiE 'bootfs|boot'; then
  echo "WARNING: no partition named bootfs/boot on $DISK. This may not be a Pi boot drive."
fi

[ -n "$OUT" ] || OUT="sigen-pi-$(date +%Y%m%d).img.xz"
[ -e "$OUT" ] && { echo "$OUT already exists; move it or name another file" >&2; exit 1; }

cat <<CONFIRM

About to READ (never write) this disk:

  identifier   /dev/r$DISK
  size         $SIZE
  name         $NAME
  partitions
$(diskutil list "$DISK" | sed 's/^/    /')

  output       $OUT

CONFIRM
printf 'Type the identifier again to confirm (%s): ' "$DISK"
read -r CONFIRMED
[ "$CONFIRMED" = "$DISK" ] || { echo "'$CONFIRMED' does not match; nothing done."; exit 1; }

# Unmounted so the bytes stop moving under the read. The raw device (/dev/rdiskN) is
# unbuffered and several times faster than the block device for a sequential read.
echo
echo "unmounting $DISK"
diskutil unmountDisk "$DISK"

BYTES=$(printf '%s' "$INFO" | sed -n 's/.*Disk Size:.*(\([0-9]*\) Bytes).*/\1/p' | head -1)

echo "reading /dev/r$DISK -> $OUT   (xz -9, this takes a while)"
if command -v pv >/dev/null 2>&1 && [ -n "$BYTES" ]; then
  sudo dd if="/dev/r$DISK" bs=4m 2>/dev/null | pv -s "$BYTES" | xz -9 -T0 > "$OUT"
else
  echo "  (no progress bar; press Ctrl-T -- macOS dd answers SIGINFO with its position)"
  sudo dd if="/dev/r$DISK" bs=4m | xz -9 -T0 > "$OUT"
fi

shasum -a 256 "$OUT" > "$OUT.sha256"

echo
echo "wrote $OUT  ($(du -h "$OUT" | cut -f1))"
echo "      $OUT.sha256"
cat <<NEXT

Write it to a drive AT LEAST AS LARGE as the one it came from, with Raspberry Pi Imager
("Use custom") or:
  xzcat $OUT | sudo dd of=/dev/rdiskN bs=4m

A written clone boots with both units installed, enabled, and NOT running -- their
ConditionPathExists has nothing to find yet. One command finishes it:
  deploy/pi/provision.sh --config-only <user>@<host>
NEXT
