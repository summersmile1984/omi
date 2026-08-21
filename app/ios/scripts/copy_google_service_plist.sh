#!/bin/sh
set -eu

target_resource_dir="${TARGET_BUILD_DIR:?}/${UNLOCALIZED_RESOURCES_FOLDER_PATH:?}"
target_plist="${target_resource_dir}/GoogleService-Info.plist"

if [ "${FIREBASE_SERVICES_ENABLED:-YES}" = "NO" ]; then
  # The target directory can survive incremental builds. A self-hosted build
  # must remove a credential copied by an earlier official build.
  rm -f "$target_plist"
  rm -rf "${target_resource_dir}/Config"
  for build_config in Base Custom prodDebug prodProfile prodRelease devDebug devProfile devRelease; do
    rm -f "${target_resource_dir}/${build_config}.xcconfig"
  done
  echo "Firebase services disabled; GoogleService-Info.plist omitted"
  exit 0
fi

case "${CONFIGURATION:?}" in
  Debug-dev | AdHoc_Staging | Release-dev | Debug | Debug-raybanDat | Profile-raybanDat | Release-raybanDat)
    source_plist="${PROJECT_DIR:?}/Config/Dev/GoogleService-Info.plist"
    ;;
  Debug-prod | Profile | Release-prod | Release)
    source_plist="${PROJECT_DIR:?}/Config/Prod/GoogleService-Info.plist"
    ;;
  *)
    echo "No GoogleService-Info.plist mapping for configuration ${CONFIGURATION}" >&2
    exit 2
    ;;
esac

if [ ! -f "$source_plist" ]; then
  echo "Missing Firebase client configuration: ${source_plist}" >&2
  exit 2
fi

mkdir -p "$target_resource_dir"
cp "$source_plist" "$target_plist"
