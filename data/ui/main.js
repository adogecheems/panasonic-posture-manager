// Thin entry point: `gjs -m data/ui/main.js [--settings]`.
//
// The application shell lives in app.js, the D-Bus handling in service.js and
// the widgets in posturePage.js / settingsPage.js.
import System from 'system';

import {run} from './app.js';

System.exit(run(ARGV));
