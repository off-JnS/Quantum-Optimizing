# Local Setup & Standalone Website Guide

> Looking for the full app manual? See [docs/USAGE.md](docs/USAGE.md).
> Hosting on a Hostinger VPS? See [docs/DEPLOY_HOSTINGER.md](docs/DEPLOY_HOSTINGER.md).
> On Windows you can also just double-click `START_APP.bat`.

## Part 1: Running Locally on Your Computer

### Option A: Simple Local Testing (Recommended for Development)

This runs the app only on your computer, at `http://localhost:8501`.

#### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

#### Step 2: Create .env File (Optional)
If you have an IBM Quantum token, create a `.env` file in the project root:
```bash
copy .env.example .env
```

Then edit `.env` and add your token:
```
IBM_QUANTUM_TOKEN=your_token_here
```

If you skip this, the app will use the free local simulator (qiskit-aer).

#### Step 3: Run the App
```bash
streamlit run app.py
```

The app opens automatically at `http://localhost:8501` in your browser.

**Keyboard shortcut**: Press `Ctrl+C` in the terminal to stop it.

---

## Part 2: Standalone Website (No VPS Needed)

If you want to make the app accessible from other devices or from the internet without buying a VPS, here are your options:

### Option 1: Local Network Access (Other Devices on Your WiFi)

Other people on your home WiFi can access the app if your computer is on.

**Already set up!** When you run `streamlit run app.py`, you'll see:
```
Local URL: http://localhost:8501
Network URL: http://<your-LAN-IP>:8501
```

Share the **Network URL** with other people on your WiFi. They can open it in their browser.

**Requirements:**
- Your computer must stay ON and running Streamlit
- Only works on your local WiFi network
- Won't work from outside your network

---

### Option 2: Make It Public Online (Ngrok - Free & Easy)

If you want the app accessible from **anywhere on the internet**, use Ngrok (a free tunneling service).

#### Step 1: Download & Install Ngrok
- Go to https://ngrok.com/download
- Download the Windows version
- Extract the `ngrok.exe` file anywhere convenient

#### Step 2: Start Streamlit
```bash
streamlit run app.py
```

#### Step 3: Open a New Terminal & Start Ngrok
In a **new** terminal:
```bash
ngrok http 8501
```

You'll see output like:
```
Forwarding    https://abc123def456.ngrok.io -> http://localhost:8501
```

Share that HTTPS link with anyone—they can open it in their browser.

**Requirements:**
- Both Streamlit AND Ngrok must be running
- Your computer must stay ON
- Internet connection needed
- Free for small usage

---

### Option 3: Free Cloud Hosting (Streamlit Cloud - Recommended for 24/7)

**Best for true 24/7 hosting without managing a computer.**

Streamlit Cloud is the official hosting for Streamlit apps. Completely free tier available.

#### Step 1: Push Code to GitHub
```bash
git push origin claude/loving-fermi-qf1fm1
```

Or create a public repo and push there.

#### Step 2: Deploy on Streamlit Cloud
1. Go to https://streamlit.io/cloud
2. Click "Deploy an app"
3. Connect your GitHub repo
4. Select the `app.py` file
5. Click "Deploy"

**Your app is now live 24/7!** No computer needs to stay on.

**Advantages:**
- Always online (24/7)
- Free tier available
- No computer hardware needed
- Easy to manage

**Limitations on free tier:**
- App goes to sleep after 7 days of inactivity (wakes up when accessed)
- Limited compute resources
- IBM Quantum token must be added as a secret

To add your IBM token to Streamlit Cloud:
1. In the app settings, go to "Secrets"
2. Add: `IBM_QUANTUM_TOKEN = your_token_here`

---

### Option 4: Other Free Cloud Platforms

If Streamlit Cloud doesn't work for you, try:

#### Railway.app
1. Sign up at https://railway.app
2. Click "Deploy from GitHub repo"
3. Select your repo + `app.py`
4. It auto-detects Python and installs dependencies
5. Sets up a public URL automatically

#### Render
1. Sign up at https://render.com
2. Create new "Web Service"
3. Connect GitHub repo
4. Set start command: `streamlit run app.py`
5. Environment variables for secrets

#### Heroku Alternatives (Fly.io)
1. Sign up at https://fly.io
2. Install `flyctl` CLI
3. Run: `fly launch` (in your project folder)
4. Run: `fly deploy`

---

## Troubleshooting

### **Error: "Port 8501 already in use"**
Something else is using port 8501. Streamlit will auto-switch to 8503, 8504, etc.

Or free the port:
```bash
netstat -ano | findstr :8501
taskkill /PID <PID_from_above> /F
```

### **Error: "ModuleNotFoundError: No module named 'qiskit'"**
Re-install dependencies:
```bash
pip install -r requirements.txt
```

### **App crashes on optimization**
- Run the self-check: `python scripts/smoke_test.py` (all lines should say PASS)
- If using IBM Quantum, ensure your token is valid in `.env`
- Try the local simulator first (leave IBM token empty)

### **Ngrok URL not working**
- Make sure Streamlit is still running (check first terminal)
- Make sure Ngrok is still running (check second terminal)
- Try clicking the URL directly from Ngrok output

### **Streamlit Cloud deployment fails**
- Ensure `requirements.txt` has all dependencies
- Make sure `.env` is in `.gitignore` (secrets go in Cloud settings instead)
- Check the deployment logs in Streamlit Cloud dashboard

---

## Summary Table

| Setup | Pros | Cons | 24/7? | Internet? |
|-------|------|------|-------|-----------|
| Local (Option A) | Simple, no account needed | Only on your PC | ❌ | ❌ |
| Local Network (Option 1) | Works on WiFi | Need WiFi network | ❌ | ❌ |
| Ngrok (Option 2) | Free, public access | Need PC + Ngrok running | ❌ | ✅ |
| Streamlit Cloud (Option 3) | Recommended, always on, free | Limited resources | ✅ | ✅ |
| Railway/Render (Option 4) | Full-featured, free tier | More setup | ✅ | ✅ |

---

## Next Steps

Choose your preferred hosting option from Part 2 above, or reach out if you need help with any specific setup!

If you have an IBM Quantum token and want to use real quantum computers, add it to your `.env` file (local) or Cloud secrets (cloud deployment).
