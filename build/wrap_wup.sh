#!/bin/bash
# Run HERE (host). Wraps the .raucb pulled from the Edge into a .wup, matching
# the exact structure of PFC-300-Linux_*.wup: zip of package-info.xml + .raucb.
set -euo pipefail

EFW=/home/wago/Documents/edge-computer-fw-update
NAME=WAGO_OS0752-9xxx_Edge_FW5_V040100_IX05
RAUCB="$EFW/bundles/$NAME.raucb"        # scp this back from /docker/edge-build/ first
STAGE="$EFW/bundles/wup-stage"

[ -f "$RAUCB" ] || { echo "missing $RAUCB - scp root@192.168.2.17:/docker/edge-build/$NAME.raucb $EFW/bundles/"; exit 1; }

rm -rf "$STAGE"; mkdir -p "$STAGE"
cp "$RAUCB" "$STAGE/$NAME.raucb"

# package-info.xml mirrors the PFC-300 one: article 0752-9xxx, Edge system.
cat > "$STAGE/package-info.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<!-- Caution! Elements and attributes in this file are case sensitive! -->
<FirmwareUpdateFile StructureVersion="1.0" Revision="1" System="Edge-Linux">
  <FirmwareDescription Revision="4.1.0" ReleaseIndex="05">
    <AssociatedFiles>
      <File RefID="RAUC-File" Type="rauc" Name="$NAME.raucb" TargetPath="/tmp/fwupdate/update_05_040100.raucb"/>
    </AssociatedFiles>
  </FirmwareDescription>
  <ArticleList>
    <Article OrderNo="0752-9xxx" GroupRef="Edge-Common"/>
  </ArticleList>
  <GroupList>
    <Group RefID="Edge-Common">
      <Upgrade>
        <VersionList>
          <VersionRange SoftwareRevision="3.0.0-4.9.99"/>
        </VersionList>
      </Upgrade>
      <Downgrade>
        <VersionList>
          <VersionRange SoftwareRevision="3.0.0-99.99.99"/>
        </VersionList>
      </Downgrade>
    </Group>
  </GroupList>
</FirmwareUpdateFile>
XML

# .wup = plain zip, package-info.xml first (as in the WAGO bundles)
( cd "$STAGE" && zip -X "$EFW/bundles/$NAME.wup" package-info.xml "$NAME.raucb" )
echo ">> wrote $EFW/bundles/$NAME.wup"
unzip -l "$EFW/bundles/$NAME.wup"
