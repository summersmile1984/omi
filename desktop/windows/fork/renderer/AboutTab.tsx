import { useEffect, useState } from 'react'
import { Globe, Info, RefreshCw } from 'lucide-react'
import { SettingRow } from '../../src/renderer/src/components/settings/SettingRow'
import { profile } from '../native/profile.generated'

export function AboutTab(): React.JSX.Element {
  const [version, setVersion] = useState<string | null>(null)
  const [versionUnavailable, setVersionUnavailable] = useState(false)
  useEffect(() => {
    let active = true
    void window.omi
      .getAppVersion()
      .then((value) => {
        if (active) setVersion(value.version)
      })
      .catch(() => {
        if (active) setVersionUnavailable(true)
      })
    return () => {
      active = false
    }
  }, [])
  const links = [
    ['Open web app', profile.links.webApp],
    ['Documentation', profile.links.docs],
    ['Help center', profile.links.help],
    ['Send feedback', profile.links.feedback],
    ['Privacy policy', profile.links.privacy],
    ['Terms of service', profile.links.terms],
    ['Service status', profile.links.status],
    ['Community', profile.links.community],
    ['Contact support', `mailto:${profile.supportEmail}`]
  ].filter(([, url]) => !!url)
  return (
    <>
      <SettingRow
        icon={Info}
        title={profile.displayName}
        subtitle={
          version
            ? `Version ${version}`
            : versionUnavailable
              ? 'Version unavailable'
              : 'Loading version…'
        }
        keywords="about version build app info"
      />
      <SettingRow
        icon={Globe}
        title="Links"
        subtitle={`Support and information for ${profile.displayName}.`}
        keywords="website documentation privacy support terms community feedback"
      >
        <div className="flex flex-col gap-3">
          {links.map(([label, url]) => (
            <a
              key={label}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-white/85 underline underline-offset-4"
            >
              {label}
            </a>
          ))}
        </div>
      </SettingRow>
      <SettingRow
        icon={RefreshCw}
        title="Software updates"
        subtitle="Automatic updates are disabled for this local test build."
        keywords="update beta release"
      />
      <p className="pt-4 text-xs text-white/60">{profile.legalEntity}</p>
    </>
  )
}
