// pm2 runs the checker once per hour (no daemon loop): the process starts on the cron tick,
// exits after ~10-60 s, and pm2 leaves it "stopped" until the next tick.
//   pm2 start ecosystem.config.js && pm2 save
const path = require('path');
const python = path.join(__dirname, '.venv', 'Scripts', 'python.exe');

module.exports = {
  apps: [{
    name: 'suliko-healthcheck',
    script: 'main.py',
    interpreter: python,
    cwd: __dirname,
    cron_restart: '0 * * * *',   // every hour, on the hour
    autorestart: false,          // do not restart when it exits; cron does that
    stop_exit_codes: [0, 1, 2],  // 0 ok, 1 failures reported, 2 alert delivery failed
    time: true,
    out_file: path.join(__dirname, 'logs', 'pm2-out.log'),
    error_file: path.join(__dirname, 'logs', 'pm2-err.log'),
    merge_logs: true,
  }],
};
