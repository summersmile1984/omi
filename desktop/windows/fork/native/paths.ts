import { app } from 'electron'
import { join } from 'node:path'
import { profile } from './profile.generated'

// This module is imported before the original main entry's module graph.
// Every lazy DB/keyring/Chromium path and single-instance lock sees this identity.
app.setName(profile.applicationId)
app.setPath('userData', join(app.getPath('appData'), profile.applicationId))
