# Deploying to Modal

1. Install Modal: `pip install modal`
2. Authenticate: `modal setup` (opens browser to log in)
3. Deploy: `modal deploy server_modal.py`
4. Copy the generated endpoint URL (Modal prints it after deploy)
5. Update the Backend URL in the extension popup to that Modal URL
6. Reload the extension in Chrome

Free tier gives $30/month compute — enough for roughly 250 class transcriptions.
Check usage at https://modal.com/usage

For local development, keep using `python server.py` (Flask on port 5001).
