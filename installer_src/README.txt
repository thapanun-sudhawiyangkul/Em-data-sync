EMBROIDERY LOG SYNC
===================
Reads the embroidery machines' log files (.MDF) from one folder and uploads
new records to Supabase. Python is NOT needed on this computer.


INSTALL ON A NEW COMPUTER
-------------------------
1. Copy the whole "EmbroiSync" folder to the computer, for example to
   C:\EmbroiSync   (keep the "app" folder inside it).
2. Double-click  Setup.bat
3. In the window:
     1. Folder     - press Browse and choose the folder with the .MDF files,
                     or type a network path such as \\MYNAS\embroidery\logs
     2. Supabase   - Project URL and secret key (already filled in if this
                     package was prepared for you)
     3. Interval   - how often to sync, in minutes
4. Press "Test settings". Every line should start with OK.
5. Press "Save and switch ON". Done - you can close the window.

The sync then runs automatically while you are logged in to Windows.
It continues from where the last upload stopped, also when that upload was
made from another computer, because the position is stored in Supabase.


CHANGE THE FOLDER LATER
-----------------------
Double-click Setup.bat, choose the new folder, press "Save and switch ON".


BUTTONS
-------
Test settings        checks the folder and Supabase, uploads nothing
Save and switch ON   saves, tests, and starts the automatic schedule
Sync now             saves and runs one sync immediately
Switch OFF           stops the automatic schedule (data and settings are kept)
Open log             shows what the sync did


FILES
-----
Setup.bat            start here
Sync now.bat         run one sync by hand and see the result
config.json          the settings written by Setup (contains the secret key)
logs\                sync_passport.log
staging\             temporary copies of the machine files
app\                 the program
supabase_schema.sql  only needed for a NEW Supabase project: paste it into
                     Supabase > SQL Editor and run it once


IF SOMETHING IS WRONG
---------------------
"data folder not found"      The folder cannot be reached from this computer.
                             Open it in Windows Explorer first. If the NAS
                             asks for a user name and password, tick
                             "Remember my credentials".
"cannot write"               The key is the publishable one. Use the secret
                             key (sb_secret_...).
"table ... missing"          Run supabase_schema.sql in Supabase once.
Windows warns about the app  Choose "More info" > "Run anyway". The program
                             is not signed.
Nothing arrives in Supabase  Press "Open log" and read the last lines.


REMOVE
------
Open Setup.bat, press "Switch OFF", close it, delete the EmbroiSync folder.
The data in Supabase is not touched.

Keep config.json private: the secret key gives full access to the database.
Use this sync on ONE computer at a time.
