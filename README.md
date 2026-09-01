Try AI directly in your favorite apps … Use Gemini to generate drafts and refine content, plus get Gemini Pro with access to Google's next-gen AI
1
100%
## For ePOS Configuration:

# Step 1: Update the list of available packages and their versions stored in the system's package index.

	sudo apt update

# Step 2: Upgrade all installed packages to their latest versions.
	sudo apt upgrade

# Step 3: Install python application dependencies
	pip3 install escpos --break-system-packages
	pip3 install pillow --upgrade --break-system-packages

# Step 4: Prepare the USB to allow communication with the thermal printer
# Create a file named printer.rules in the home directory 

	nano printer.rules

# Place the following contents in the printer.rules file

	SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5743", MODE="666"

# Move the printer.rules file to the /etc/udev/rules.d directory:

	sudo mv printer.rules /etc/udev/rules.d

# Reload the udev rules and trigger the new configuration: It is important to create apassword for admin before reloading the udev rules.


	sudo passwd

# Enter the password: Ecobarter2025

# Switch to Super user by entering the next command:

	su

# reload the udev rules:

	udevadm control --reload-rules && udevadm trigger

	exit

# Step 5: Copy the Ecobarter POS files to the Raspberry pi home folder Note: You will find these files in the ecobarter RVM deployment folder. Copy all the files and also copy the assets folder to the home directory of the Raspberry pi.

# Step 6: Setup Auto-start: Open terminal and create a new service file:

	sudo nano /etc/systemd/system/rvmapp-script.service

# Add the following commands to the rvmapp-script.service file:

	[Unit]
	Description=Run Python Script at Boot
	After=multi-user.target
	[Service]
	Environment=DISPLAY=:0
	Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native
	Environment=XDG_RUNTIME_DIR=/run/user/1000
	ExecStart=/usr/bin/python3 /home/ecorvmpi/app.py
	Restart=always
	RestartSec=5
	User=ecorvmpi
	WorkingDirectory=/home/ecorvmpi
	StandardOutput=journal
	StandardError=journal
	[Install]
	WantedBy=multi-user.target



# Save the file with Ctrl+X then press y to accept the file name.

# Enable the service:

	sudo systemctl enable scale-script.service

# Start the service:

	sudo systemctl start scale-script.service


# Setup Cron to Upload Data To Server

# Open Terminal and setup the cron: - add a cron job that runs by the hour on every SmartScale to push out data uploads to rvmCpanel every 6 hours - api.py

	crontab -e

# Add the python3 command with the file name at the end of the file on a new line.

	0 */6 * * * python3 api.py


# Further Development Direction

	Work on disabling Screen Orientation on the raspberry pi touchscreen to make going into full screen mode easy...
	Tips: Ask ChatGPT
