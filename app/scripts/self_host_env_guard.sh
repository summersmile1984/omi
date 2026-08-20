#!/usr/bin/env bash

# Run a complete self-hosted Flutter codegen/build callback with a sanitized
# Envied input, then restore every developer-owned/generated file even on
# failure. Source this file and call with_self_host_env_guard <app-root> <cmd...>.
with_self_host_env_guard() (
  local app_root="${1:?app root is required}"
  shift
  local env_file="${app_root}/.env"
  local generated_file="${app_root}/lib/env/prod_env.g.dart"
  local analysis_file="${app_root}/analysis_options.yaml"
  local lock_file="${app_root}/pubspec.lock"
  local guard_dir
  guard_dir="$(mktemp -d "${TMPDIR:-/tmp}/omi-selfhost-env.XXXXXX")"
  local had_env=false had_generated=false had_analysis=false had_lock=false

  if [[ -f "$env_file" ]]; then cp -p "$env_file" "$guard_dir/env"; had_env=true; fi
  if [[ -f "$generated_file" ]]; then cp -p "$generated_file" "$guard_dir/generated"; had_generated=true; fi
  if [[ -f "$analysis_file" ]]; then cp -p "$analysis_file" "$guard_dir/analysis"; had_analysis=true; fi
  if [[ -f "$lock_file" ]]; then cp -p "$lock_file" "$guard_dir/lock"; had_lock=true; fi

  umask 077
  {
    printf 'API_BASE_URL=%s\n' "${OMI_API_BASE_URL:?OMI_API_BASE_URL is required for self-host codegen}"
    printf 'USE_WEB_AUTH=false\n'
    printf 'USE_AUTH_CUSTOM_TOKEN=false\n'
  } > "$env_file"

  restore_self_host_env() {
    [[ -d "$guard_dir" ]] || return 0
    if [[ "$had_env" == true ]]; then rm -f "$env_file"; cp -p "$guard_dir/env" "$env_file"; else rm -f "$env_file"; fi
    if [[ "$had_generated" == true ]]; then rm -f "$generated_file"; cp -p "$guard_dir/generated" "$generated_file"; else rm -f "$generated_file"; fi
    if [[ "$had_analysis" == true ]]; then rm -f "$analysis_file"; cp -p "$guard_dir/analysis" "$analysis_file"; else rm -f "$analysis_file"; fi
    if [[ "$had_lock" == true ]]; then rm -f "$lock_file"; cp -p "$guard_dir/lock" "$lock_file"; else rm -f "$lock_file"; fi
    rm -rf "$guard_dir"
  }
  trap 'restore_self_host_env; exit 130' INT TERM HUP
  set +e
  "$@"
  callback_status=$?
  set -e
  lock_changed=false
  if [[ "$had_lock" == true ]]; then
    if [[ ! -f "$lock_file" ]] || ! cmp -s "$guard_dir/lock" "$lock_file"; then
      lock_changed=true
    fi
  elif [[ -e "$lock_file" ]]; then
    lock_changed=true
  fi
  if [[ "$lock_changed" == true ]]; then
    echo "self-host build changed pubspec.lock; use the repository-pinned Flutter SDK and --enforce-lockfile" >&2
    if [[ "$callback_status" -eq 0 ]]; then callback_status=1; fi
  fi
  restore_self_host_env
  trap - INT TERM HUP
  exit "$callback_status"
)
