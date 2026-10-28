# Self-hosting Supabase on the sync PC

Moves the database from Supabase cloud to the Windows PC that runs the sync.
Supabase self-hosted runs as Docker containers. On Windows that means
Docker Desktop on top of WSL 2 (a small Linux layer), with the Supabase files
living inside the Linux side.

Written 2026-10-14 from the official `docker/setup.sh` in the Supabase
repository. The Linux-side steps were read, not rehearsed, because the dev PC
has no WSL. Official guide: https://supabase.com/docs/guides/self-hosting/docker

## Requirements on the PC

| item | needed |
|---|---|
| Windows 10 21H2+ or Windows 11, 64-bit | any edition, Home is fine |
| RAM | 8 GB minimum, 16 GB comfortable |
| Free disk | 20 GB |
| CPU virtualization | enabled in BIOS (usually is) |
| Internet | for the one-time download of about 3 GB of images |
| Windows login | an account with administrator rights for the install |

## Part 1: WSL 2 and Docker Desktop

1. Open **PowerShell as administrator** (right-click Start, "Terminal (Admin)").
2. Install WSL with Ubuntu:
   ```
   wsl --install
   ```
   Reboot when asked. After the reboot an Ubuntu window opens and asks for a
   new Linux username and password. Choose simple ones and write them down.
3. Download and install **Docker Desktop for Windows** from
   https://www.docker.com/products/docker-desktop/ with the default options
   ("Use WSL 2"). Sign-in to a Docker account is optional, skip it.
4. In Docker Desktop: Settings > General, tick **"Start Docker Desktop when
   you sign in to your computer"**. Settings > Resources > WSL integration,
   make sure **Ubuntu** is enabled. Apply and restart.
5. Open the **Ubuntu** app from the Start menu and check Docker is visible:
   ```
   docker --version
   docker compose version
   ```
   Both must print a version. If not, Docker Desktop is not running or WSL
   integration is off.

## Part 2: install Supabase

Everything in this part is typed in the **Ubuntu** window.

1. Tools the setup script needs:
   ```
   sudo apt update && sudo apt install -y git jq openssl curl
   ```
2. Download and run the official setup script. It copies the Supabase Docker
   files into a `supabase` folder, generates all passwords and API keys, and
   pulls the images (several minutes):
   ```
   cd ~
   curl -fsSL https://raw.githubusercontent.com/supabase/supabase/master/docker/setup.sh -o setup.sh
   sh setup.sh --skip-deps --project-dir supabase
   ```
   `--skip-deps` is important: Docker already comes from Docker Desktop.
   The script asks three URLs. Press Enter for the defaults
   (`http://localhost:8000`) if only this PC will use the database. If you want
   to open the dashboard from your own PC as well, answer the first question
   with `http://<ip-of-this-pc>:8000` and keep the other two defaults.
3. Start it:
   ```
   cd ~/supabase
   sh run.sh up -d
   ```
   The first start takes a few minutes. Check:
   ```
   docker compose ps
   ```
   Every service should show `running` or `healthy`. If one shows
   `restarting`, wait two minutes and check again.
4. Read your keys and dashboard login:
   ```
   grep -E '^(DASHBOARD_USERNAME|DASHBOARD_PASSWORD|SUPABASE_SECRET_KEY|SUPABASE_PUBLISHABLE_KEY)=' .env
   ```
   Copy these somewhere safe. `SUPABASE_SECRET_KEY` (starts with
   `sb_secret_`) is what the sync will use.

## Part 3: create the tables

1. In a browser on the PC open http://localhost:8000 and log in with
   `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`.
2. Open **SQL Editor**, paste the whole `supabase_schema.sql` (it is inside
   `C:\EmbroiSync`), press Run. It must finish without errors.

## Part 4: point the sync at the local server

1. Open `C:\EmbroiSync\config.json` in Notepad and change two lines:
   ```
   "url": "http://localhost:8000",
   "key": "sb_secret_....",        <- SUPABASE_SECRET_KEY from .env
   ```
   Leave `data_dir` and everything else as it is. Save.
   (The setup window in packages built before 2026-10-14 refuses `http://`
   addresses. Editing the file directly avoids that; newer packages accept it.)
2. Double-click `C:\EmbroiSync\Sync now.bat`. Expected: each machine shows
   `-> FULL, sending N rows` followed by `done: N new rows`, because the local
   database starts empty. This re-uploads everything the machine files still
   contain, so no history is lost as long as the machines have not cleared
   their own logs.
3. In the dashboard, **Table Editor** > `passport_log` should now have rows,
   and `passport_sync_state` one row per machine.
4. The scheduled task keeps running as before; it simply uses the new
   address from now on.

## Part 5: backups (do not skip)

The whole database now lives on one PC. Make a daily dump to a second place,
for example the NAS.

1. In Ubuntu, create the backup script:
   ```
   mkdir -p /mnt/c/SupabaseBackups
   cat > ~/backup-supabase.sh <<'EOF'
   #!/bin/sh
   cd ~/supabase
   f=/mnt/c/SupabaseBackups/supabase-$(date +%Y%m%d).sql.gz
   docker compose exec -T db pg_dump -U postgres -d postgres --clean --if-exists | gzip > "$f"
   find /mnt/c/SupabaseBackups -name 'supabase-*.sql.gz' -mtime +30 -delete
   echo "backup written: $f"
   EOF
   chmod +x ~/backup-supabase.sh
   ~/backup-supabase.sh
   ```
   A file should appear in `C:\SupabaseBackups`.
2. Schedule it from Windows. In PowerShell (normal user):
   ```
   $a = New-ScheduledTaskAction -Execute "wsl.exe" -Argument '-d Ubuntu -- sh -lc "~/backup-supabase.sh"'
   $t = New-ScheduledTaskTrigger -Daily -At 02:00
   Register-ScheduledTask -TaskName "Supabase backup" -Action $a -Trigger $t -Force
   ```
3. Copy `C:\SupabaseBackups` to the NAS with your existing copy job, or point
   the script at a mounted NAS path instead of `C:\SupabaseBackups`.

Restore, if ever needed: `zcat backup.sql.gz | docker compose exec -T db psql -U postgres -d postgres`.

## Daily operation

- **Start / stop**: Docker Desktop starts at sign-in and brings the containers
  back. Manual: `sh run.sh up -d` and `sh run.sh down` in `~/supabase`.
- **Dashboard**: http://localhost:8000 on that PC.
- **From your own PC**: only if you chose the IP address in Part 2 step 2.
  Then also allow port 8000 in Windows Firewall:
  `netsh advfirewall firewall add rule name="Supabase" dir=in action=allow protocol=TCP localport=8000`
- **Updates**: back up first, then in `~/supabase` run `sh update.sh`.
- **The PC must stay on and logged in** for both the sync and the database.

## Switching back to the cloud

Change `url` and `key` in `config.json` back to the cloud values and run
`Sync now.bat`. The cloud project keeps its own `passport_sync_state`, so the
sync resumes from the cloud's last position.

## Notes

- `PGRST_DB_MAX_ROWS=1000` in `.env` caps API reads at 1,000 rows per request.
  The sync never reads more than that at once. Raise it if a dashboard needs to.
- Supabase cloud and self-hosted use the same API, so nothing in the sync
  program changes except the address and key.
- Cloud alternatives that are lighter than a full self-host: keep the free
  cloud project, or run only Postgres in Docker and switch the sync to a
  direct database connection. Ask if you want the second one.
