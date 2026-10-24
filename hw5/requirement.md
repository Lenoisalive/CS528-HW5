In this homework we will build on top of the work from Homework 4
1. I will assume that you have successfully built the 2 apps required in HW 4.  If not please reach out to me or the TAs for code on which to build.
2. You should add a Cloud SQL database to your project and create a schema that can hold the following information about the requests that your web server is receiving: country, client ip, gender, age, income, is_banned, time of day, and requested file. Look at the requests the client is sending to discover the corresponding headers associated with this information. Make sure your tables have a primary key defined!
3. You should modify your web server to extract the information from the incoming requests, process it appropriately, and insert it into the database.  You will need to make the appropriate changes to your VM so that it has the right permissions and installed libraries to be able to talk to your database.  Requests that fail (return codes other than 200) should be logged into an additional separate table that only contains the time of request, the requested file, and the error code.
4. Demonstrate the functionality of your app by using curl to issue one successful and 2 erroneous requests and show the contents of your tables before and after each request.
5. Provision a VM that can handle the request load from 2 concurrent clients (use the client that has been provided to you with this assignment) and run each client to issue 50,000 requests against your server.  Make sure to start the 2 clients with the same random seed so the clients are consistent about their request generation.  This may take a little while to finish (up to 2-3 hours depending on the code you have written)
6. Instrument your webserver using the high accuracy python timing service and report how much time it takes to process incoming requests and extract their headers, how much time it takes to read the serving files from cloud storage, how much time it takes to send responses to the clients, and how much time it takes to enter the data in the database.  Make sure those operations are in separate functions so they can be easily instrumented.
7. Once the clients have completed, compute the following statistics on your requests:
  - How many requests were you able to process successfully vs unsuccessfully?
  - How many requests came from banned countries? 
  - How many requests were made by Male vs Female users?
  - What were the top 5 countries sending requests to your server?
  - What age group issued the most requests to your server?
  - What income group issued the most requests to your server?
8. It is very important that you don’t leave your database running when not in actual use since it is a very expensive resource and can deplete your account credits. You don’t have to delete it, just stop it to save money.  Write a cloud function that wakes every 1 hour and checks the database if it’s running and stops it if this is the case. If you are actively working and using the database disable the  cloud function for that period and remember to re-enable it when done.
9. Write a set of scripts for creating, starting and stopping the various resources you need.  See details below.

What to turn in:
- The python code for your modified first app, the unmodified second app, and the cloud function that monitors your database  as a github link.
- A pdf file describing all the necessary steps to configure and run your apps, your database schema,  screenshots of your curl work from step #4, and your answers to parts #6 and #7.
- In your report include your total spend on cloud resources for the development and execution of this program(s).  Due to the delays in the billing system, it is ok for you to add this as a separate file (billing_repord.pdf) in your github repository the day after the assignment is due.


Automated Setup and Cleanup Scripts
For HW5, you are required to include a setup.sh and a cleanup.sh script alongside your homework files. These scripts automate the deployment and teardown of your environment, which is how your submission will be graded.
Important Grading Note: A small amount of points will be deducted if the grader script fails due to permission issues, or if your setup.sh and cleanup.sh files are not properly organized in the correct directory. Please verify that your scripts run exactly as intended from a fresh environment.
1. Infrastructure Scripts (setup.sh and cleanup.sh)
- setup.sh: This script must provision the infrastructure required for your homework.
  - Project ID: For this homework, you must hardcode your GCP Project ID in your scripts. Do not try to fetch the project dynamically (e.g., do not use gcloud config get-value project), as this will pull the grader's project ID during testing and fail.
  - Cloud SQL Setup: Your script should include a function that handles your Cloud SQL database. It needs to check if your table/database already exists. If it exists, your script should simply turn on (start) the Cloud SQL server and skip creation. If it does not exist, it should create/start the server and then call a Python script to set up the database schema.
  - Static IPs: If your setup requires a static IP, do not hardcode the actual IP address value (e.g., 34.12.34.56) in your scripts. Instead, allocate the IP and use a gcloud command to retrieve its value into a variable dynamically.
- cleanup.sh: This script must tear down the infrastructure created by your setup.sh to prevent ongoing billing.
  - Cloud SQL Exception: Do NOT delete your Cloud SQL server in the cleanup script. Instead, just stop the server.
  - Static IPs: Make sure your cleanup script explicitly releases and removes any static IPs you generated during setup.
  - Auth: If you use gcloud auth application-default login, be sure to include gcloud auth application-default revoke.
2. VM-Specific Instructions (startup.sh)
If your assignment involves creating a Virtual Machine to interact with your database, you must provide a startup.sh script to configure the VM internally upon launch.
Integration: Pass this startup.sh script during VM creation using the metadata flag:
gcloud compute instances create ... --metadata-from-file startup-script=startup.sh ...
Important Restriction: You should not use gcloud compute ssh --command= to set up your VMs. All internal configuration must happen via your startup.sh script.

Run-Once Logic: Because the startup script runs every time the VM boots, you should prevent it from reinstalling packages on subsequent reboots. You can do this by checking for an empty lock file. (Note: The startup script automatically runs as root, so you do not need to use sudo).
# Check if setup has already occurred
if [ -f /var/log/startup_already_done ]; then
    echo "Startup script already ran once. Skipping."
    exit 0
fi

# Your installation and setup commands go here
# ...
apt install git python3-pip # notice the lack of sudo

# Create the lock file to prevent future runs
touch /var/log/startup_already_done
3. GitHub Repository Structure
To ensure your submission can be tested correctly, your repository must follow this exact directory structure. Note: The folder name must be hw5 in lowercase.

repository/
├── hw1/
├── ...
├── hw5/
│   ├── setup.sh
│   ├── cleanup.sh
│   ├── startup.sh
│   ├── setup_schema.py 
│   └── ... (your other homework files)
4. AI Usage
The bash scripts themselves are pretty straightforward. Feel free to ask your favorite LLM to help you write the syntax for setup.sh, cleanup.sh, and startup.sh.

