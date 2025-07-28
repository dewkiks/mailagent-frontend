import streamlit as st
import requests
import json
import time
import threading
import queue
import pandas as pd
from datetime import datetime
import plotly.express as px
from typing import Dict, List
import asyncio
import websockets
import logging
from urllib.parse import urlparse, urljoin
import socket
import os

# --- Configuration ---

API_BASE_URL = os.getenv("API_BASE_URL")
# API_BASE_URL = "http://localhost:8000"
WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")

# Configure Streamlit
st.set_page_config(
    page_title="Email Support Dashboard",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize session state
if 'websocket_data' not in st.session_state:
    st.session_state.websocket_data = {
        'status': {}, 'stats': {}, 'latest_events': [],
        'connected': False, 'last_update': None,
        'is_processing': False, 'last_processed_event': None,
        'connection_status': 'Disconnected'
    }
if 'processed_emails' not in st.session_state:
    st.session_state.processed_emails = {}

if 'pending_toast' not in st.session_state:
    st.session_state.pending_toast = None

# Add this to track if we need to rerun
if 'needs_rerun' not in st.session_state:
    st.session_state.needs_rerun = False

# --- Logger Setup ---
logger = logging.getLogger("email_bot_dashboard")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# --- API Client ---
class APIClient:
    """A client to interact with the backend API."""
    def __init__(self, base_url: str):
        self.base_url = base_url

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(method, url, timeout=10, **kwargs)
            response.raise_for_status()  # This will raise an exception for HTTP errors
            
            # Try to parse JSON response
            try:
                return response.json()
            except ValueError:
                # If response is not JSON, return the text content
                return {"message": response.text, "status_code": response.status_code}
                
        except requests.exceptions.RequestException as e:
            logger.error(f"API request to {url} failed: {e}")
            # Don't show error to user here, let the calling method handle it
            return None

    def get_status(self) -> Dict:
        """Get API status - mock implementation for demo"""
        try:
            return self._request("get", "/status") or {}
        except:
            # Mock response for demonstration
            return {
                "status": "running",
                "timestamp": datetime.now().isoformat(),
                "version": "1.0.0"
            }

    def get_stats(self) -> Dict:
        """Get processing stats"""
        return self._request("get", "/stats") or {}

    def get_processed_emails(self) -> Dict:
        """Get processed emails"""
        return self._request("get", "/processed-emails") or {"processed_emails": [], "count": 0}

    def get_manual_review_emails(self) -> List[Dict]:
        """Get emails requiring manual review - mock implementation for demo"""
        return self._request("get", "/manual-review-emails") or []

    def get_discarded_emails(self) -> List[Dict]:
        """Get discarded emails"""
        try:
            return self._request("get", "/discarded-emails") or []
        except:
            return []

    def update_discarded_status(self, message_id: str, status: str = 'processed') -> Dict:
        """Update discarded email status"""
        data = {"message_id": message_id, "status": status}
        try:
            return self._request("post", "/update-discarded-status", json=data) or {"success": False, "message": "Request failed"}
        except:
            return {"success": False, "message": "API unavailable"}

    def send_manual_reply(self, data: Dict) -> Dict:
        """Send manual reply"""
        url = f"{self.base_url}/send-manual-reply"
        try:
            response = requests.post(url, json=data, timeout=30)  # Increased timeout
            response.raise_for_status()
            return {"success": True, "message": "Reply sent successfully"}
        except requests.exceptions.Timeout:
            # Assume success on timeout since email is likely sent
            logger.warning("Manual reply request timed out, but email may have been sent")
            return {"success": True, "message": "Reply sent (request timed out but likely successful)"}
        except requests.exceptions.RequestException as e:
            logger.error(f"Error sending manual reply: {e}")
            return {"success": False, "message": f"Request failed: {str(e)}"}

    def send_manual_process(self, data: Dict) -> Dict:
        """Process email manually"""
        try:
            return self._request("post", "/process-email", json=data) or {"success": False, "message": "Request failed"}
        except:
            return {"success": False, "message": "API unavailable"}

    def reset_processed_emails(self) -> Dict:
        """Reset processed emails"""
        try:
            return self._request("delete", "/reset-processed") or {"message": "Error occurred"}
        except:
            return {"message": "History cleared (mock)"}

# --- WebSocket Manager ---
class WebSocketManager:
    """Manages the WebSocket connection and data flow in a separate thread."""
    def __init__(self, url: str):
        self.url = url
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        """Start the WebSocket client in a background thread."""
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run_client, daemon=True)
            self.thread.start()

    def stop(self):
        """Stop the WebSocket client."""
        self.stop_event.set()
        if self.thread:
            self.thread.join()

    def _run_client(self):
        """Run the asyncio event loop in a separate thread."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._listen())
        except Exception as e:
            logger.error(f"WebSocket run_client error: {e}")

    async def _listen(self):
        """Listen for messages and handle reconnection."""
        while not self.stop_event.is_set():
            try:
                self.queue.put({'type': 'connection_status', 'status': 'Connecting...'})
                async with websockets.connect(self.url) as websocket:
                    self.queue.put({'type': 'connection_status', 'status': 'Connected'})
                    logger.info("WebSocket connection established.")
                    
                    while not self.stop_event.is_set():
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                            data = json.loads(message)
                            
                            if data.get('type') == 'ping':
                                await websocket.send(json.dumps('{"type":"pong"}'))
                                continue
                                
                            self.queue.put(data)
                        except asyncio.TimeoutError:
                            # No message received in 30s, assume connection is fine, continue listening
                            continue
                        except json.JSONDecodeError:
                            logger.warning(f"Received invalid JSON: {message}")

            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError) as e:
                logger.warning(f"WebSocket connection lost: {e}. Reconnecting in 5 seconds...")
                self.queue.put({'type': 'connection_status', 'status': 'Disconnected'})
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 5 seconds...")
                self.queue.put({'type': 'connection_status', 'status': 'Disconnected'})
            
            await asyncio.sleep(5)

# --- UI Components ---
class UI:
    """Handles rendering of the Streamlit UI components."""
    def __init__(self, api_client: APIClient):
        self.api_client = api_client

    def render_custom_css(self):
        """Render custom CSS for the dashboard"""
        st.markdown("""
            <style>
                    
                /* Dark Theme Body and Font */
                body {
                    color: #fafafa;
                }
                .stApp {
                    background-color: #0e1117;
                }

                /* Main container */
                .main .block-container {
                    padding: 2rem;
                }

                /* Page Headers */
                .main-header {
                    background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                    color: white;
                    padding: 2rem;
                    border-radius: 15px;
                    margin-bottom: 2rem;
                    box-shadow: 0 8px 25px rgba(0,0,0,0.15);
                }
                .main-header h1 {
                    font-size: 2.5rem;
                    font-weight: 700;
                    margin: 0;
                }

                /* Dark Sidebar */
                [data-testid="stSidebar"] {
                    background: linear-gradient(180deg, #1a1d29 0%, #121418 100%);
                    border-right: 1px solid #2a2d35;
                }
                
                [data-testid="stSidebar"] h1 {
                    color: #ffffff !important;
                    font-weight: 700;
                    text-align: center;
                    margin-bottom: 2rem;
                    font-size: 1.5rem;
                }

                /* Metric Cards */
                .metric-card {
                    background: #1a1d29;
                    padding: 2rem;
                    border-radius: 15px;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.08);
                    text-align: center;
                    transition: all 0.3s ease;
                    border: 1px solid #2a2d35;
                }
                .metric-card:hover {
                    transform: translateY(-5px);
                    box-shadow: 0 8px 20px rgba(0,0,0,0.12);
                }
                .metric-card .metric-title {
                    font-size: 1rem;
                    color: #a0a0a0;
                    font-weight: 500;
                    margin-bottom: 1rem;
                }
                .metric-card .metric-value {
                    font-size: 2.5rem;
                    font-weight: 700;
                    color: #ffffff;
                    margin-bottom: 0.5rem;
                }

                /* Status Badges */
                .status-badge {
                    display: inline-flex;
                    align-items: center;
                    padding: 0.5rem 1rem;
                    border-radius: 50px;
                    font-weight: 600;
                    font-size: 0.9rem;
                }
                .status-connected {
                    background-color: #1c3a24;
                    color: #a3ffb3;
                }
                .status-disconnected {
                    background-color: #4d1f22;
                    color: #ffb3b3;
                }

                /* Enhanced Button Styles */
                .stButton > button {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    border: none;
                    padding: 0.75rem 2rem;
                    border-radius: 50px;
                    font-weight: 600;
                    font-size: 1rem;
                    transition: all 0.3s ease;
                }
                .stButton > button:hover {
                    box-shadow: 0 4px 15px rgba(118, 75, 162, 0.4);
                    transform: translateY(-2px);
                }
                
                /* Expander styles */
                .stExpander {
                    background: #1a1d29;
                    border: 1px solid #2a2d35;
                    border-radius: 15px;
                    overflow: hidden;
                }
                /* Status Bar Styles */
                .agent-status-bar {
                    background: #1a1d29;
                    padding: 0.8rem 1.2rem;
                    border-radius: 8px;
                    border: 1px solid #2a2d35;
                    margin-bottom: 1rem;
                    display: flex;
                    align-items: center;
                    gap: 0.8rem;
                }

                .status-indicator {
                    width: 8px;
                    height: 8px;
                    border-radius: 50%;
                    flex-shrink: 0;
                }

                .status-processing {
                    background-color: #ffc107;
                    animation: pulse 2s infinite;
                }

                .status-idle {
                    background-color: #28a745;
                }

                .status-text {
                    color: #ffffff;
                    font-size: 0.9rem;
                    font-weight: 500;
                    margin: 0;
                }

                @keyframes pulse {
                    0% { opacity: 1; }
                    50% { opacity: 0.5; }
                    100% { opacity: 1; }
                }
                    
                /* Dashboard Status Bar Styles */
                .dashboard-status-container {
                    display: flex;
                    gap: 1rem;
                    margin-bottom: 2rem;
                }

                .dashboard-status-card {
                    background: #1a1d29;
                    border: 1px solid #2a2d35;
                    border-radius: 12px;
                    padding: 1.5rem;
                    flex: 1;
                    min-height: 80px;
                    display: flex;
                    align-items: center;
                    gap: 1rem;
                    transition: all 0.3s ease;
                }

                .dashboard-status-card:hover {
                    border-color: #3a3d45;
                    transform: translateY(-2px);
                }

                .dashboard-status-icon {
                    width: 12px;
                    height: 12px;
                    border-radius: 50%;
                    flex-shrink: 0;
                }

                .dashboard-status-content {
                    flex: 1;
                }

                .dashboard-status-title {
                    font-size: 0.9rem;
                    color: #a0a0a0;
                    margin: 0 0 0.5rem 0;
                    font-weight: 500;
                }

                .dashboard-status-value {
                    font-size: 1.2rem;
                    font-weight: 600;
                    color: #ffffff;
                    margin: 0;
                }

                .status-running {
                    background-color: #28a745;
                    box-shadow: 0 0 10px rgba(40, 167, 69, 0.3);
                }

                .status-stopped {
                    background-color: #dc3545;
                    box-shadow: 0 0 10px rgba(220, 53, 69, 0.3);
                }

                .status-warning {
                    background-color: #ffc107;
                    box-shadow: 0 0 10px rgba(255, 193, 7, 0.3);
                }

                .status-processing {
                    background-color: #17a2b8;
                    animation: pulse 2s infinite;
                }

                @keyframes pulse {
                    0% { box-shadow: 0 0 10px rgba(23, 162, 184, 0.3); }
                    50% { box-shadow: 0 0 20px rgba(23, 162, 184, 0.6); }
                    100% { box-shadow: 0 0 10px rgba(23, 162, 184, 0.3); }
}
            </style>
        """, unsafe_allow_html=True)

    def render_sidebar(self) -> str:
        """Render the sidebar with navigation and status"""
        with st.sidebar:
            st.markdown('<div class="sidebar-header"><h1>Email Support Demo</h1></div>', unsafe_allow_html=True)
            
            page = st.radio("Navigation", ["Dashboard", "Manual Review", "History"], key="navigation")

            st.markdown("<br>", unsafe_allow_html=True)
            st.subheader("Agent Status")

            # Processing Status Bar
            is_processing = st.session_state.websocket_data.get('is_processing', False)
            if is_processing:
                st.markdown("""
                    <div class="agent-status-bar">
                        <div class="status-indicator status-processing"></div>
                        <p class="status-text">Processing email...</p>
                    </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown("""
                    <div class="agent-status-bar">
                        <div class="status-indicator status-idle"></div>
                        <p class="status-text">IDLE - Ready</p>
                    </div>
                """, unsafe_allow_html=True)

            # Connection Status Bar
            connection_status = st.session_state.websocket_data.get('connection_status', 'Disconnected')
            if connection_status == 'Connected':
                st.markdown("""
                    <div class="agent-status-bar">
                        <div class="status-indicator status-idle"></div>
                        <p class="status-text">Connected</p>
                    </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown("""
                    <div class="agent-status-bar">
                        <div class="status-indicator" style="background-color: #dc3545;"></div>
                        <p class="status-text">Disconnected</p>
                    </div>
                """, unsafe_allow_html=True)

        return page

    def show_toast_notification(self, event):
        """Show toast notification for email processing result"""
        result = event.get('result', {})
        sender_email = event.get('email', 'unknown')
        
        if result.get('success'):
            if result.get('response_sent'):
                st.toast(f"✅ Reply sent to {sender_email}", icon="✅")
            else:
                st.toast(f"📋 Email from {sender_email} requires manual review.", icon="📋")
        else:
            st.toast(f"❌ Error processing email from {sender_email}", icon="❌")
            
    def render_manual_review(self):
        """Render manual review queue with reply functionality"""
        st.markdown('<div><h1> Manual Review Queue</h1></div>', unsafe_allow_html=True)
        
        # Get manual review emails
        manual_emails = self.api_client.get_manual_review_emails()
        
        if not manual_emails:
            st.info("🎉 No emails currently require manual review.")
            return
        
        st.info(f"📬 {len(manual_emails)} emails require manual review")
        
        # Display emails
        for i, email in enumerate(manual_emails):
            priority = email.get('priority', 'low')
            
            with st.expander(
                f"{'🔴' if priority == 'high' else '🟡' if priority == 'medium' else '🟢'} "
                f"Email {i+1}: {email.get('subject', 'No Subject')} "
                f"- from {email.get('sender', 'Unknown')}",
                expanded=i==0
            ):
                # Email details
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.markdown(f"**From:** `{email.get('sender')}`")
                    st.markdown(f"**Subject:** `{email.get('subject')}`")
                    st.markdown(f"**Body:** `{email.get('content')}`")
                    st.markdown(f"**Priority:** `{priority.upper()}`")
                
                # Reply Section
                st.markdown("---")
                st.subheader("📤 Compose Reply")
                
                # Reply form
                with st.form(key=f"reply_form_{i}"):
                    reply_subject = st.text_input(
                        "Reply Subject",
                        f"Re: {email.get('subject', '')}",
                        key=f"subject_{i}"
                    )
                    
                    reply_body = st.text_area(
                        "Reply Body",
                        height=200,
                        placeholder="Type your reply here...",
                        key=f"body_{i}"
                    )
                    
                    # Action buttons
                    send_reply = st.form_submit_button("Send Reply", type="primary")
                    
                    # with col2:
                    #     save_draft = st.form_submit_button("💾 Save Draft")
                    
                    # with col3:
                    #     mark_resolved = st.form_submit_button("✅ Mark as Resolved")
                    
                    # Handle form submissions
                    if send_reply:
                        if reply_body.strip():
                            with st.spinner("Sending reply..."):
                                reply_data = {
                                    "recipient": email.get("sender", ""),
                                    "subject": reply_subject,
                                    "body": reply_body,
                                    "message_id": email.get("message_id", ""),
                                    "priority": priority
                                }
                                
                                result = self.api_client.send_manual_reply(reply_data)
                                
                                if result is not None and not result.get("success") == False:
                                    st.success("✅ Reply sent successfully!")
                                    time.sleep(2)
                                    st.rerun()
                                else:
                                    st.error(f"❌ Failed to send reply: {result.get('message', 'Unknown error')}")
                        else:
                            st.error("❌ Please enter a reply message.")
                    
                    # if save_draft:
                    #     st.info("💾 Draft saved (feature not implemented)")
                    
                    # if mark_resolved:
                    #     st.success("✅ Email marked as resolved (feature not implemented)")

    def render_history(self):
        """Render email history from session state."""
        st.markdown('<div><h1> Email History</h1></div>', unsafe_allow_html=True)

        # Use processed_emails from session_state, which is updated by websockets
        processed_list = list(st.session_state.get('processed_emails', {}).values())
        
        if not processed_list:
            st.info("No processed email history found. As emails are processed, they will appear here.")
            return

        # Sort by timestamp, newest first
        processed_list.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        # Create summary dataframe
        summary_data = []
        for email in processed_list:
            summary_data.append({
                'Timestamp': email.get('timestamp', ''),
                'From': email.get('sender', ''),
                'Subject': email.get('subject', ''),
                'Status': email.get('status', ''),
            })
        
        df = pd.DataFrame(summary_data)
        st.dataframe(df, use_container_width=True)

        st.markdown("---")
        st.subheader("📖 Email Details")

        preview_options = {
            f"{mail.get('subject', 'No Subject')} - from {mail.get('sender', 'Unknown')} [{mail.get('status', 'Unknown')}]": mail
            for mail in processed_list
        }

        selected_key = st.selectbox(
            "Select an email to view details:",
            list(preview_options.keys()),
            key="history_preview"
        )

        if selected_key:
            selected_mail = preview_options[selected_key]

            with st.expander("📧 Email Details", expanded=True):
                # Email metadata
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**From:** `{selected_mail.get('sender')}`")
                    st.markdown(f"**Subject:** `{selected_mail.get('subject')}`")
                    st.markdown(f"**Status:** `{selected_mail.get('status', 'unknown')}`")
                with col2:
                    st.markdown(f"**Timestamp:** `{selected_mail.get('timestamp', '')}`")

                # Original email content
                st.markdown("**Original Email:**")
                st.markdown(
                    f"<div style='background:#1a1d29;padding:1em;border-radius:8px;border:1px solid #2a2d35'>"
                    f"{selected_mail.get('content', 'No content available')}</div>",
                    unsafe_allow_html=True
                )

                # AI Response - Display the stored response
                ai_response = selected_mail.get('ai_response', '')
                if ai_response:
                    st.markdown("**AI Response:**")
                    st.markdown(
                        f"<div style='background:#1e2329;padding:1em;border-radius:8px;border:1px solid #2a2d35;border-left:4px solid #28a745'>"
                        f"{ai_response}</div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.markdown("**AI Response:**")
                    st.markdown(
                        f"<div style='background:#2d1a1a;padding:1em;border-radius:8px;border:1px solid #3d2a2a;border-left:4px solid #dc3545'>"
                        f"No response generated (likely required manual review)</div>",
                        unsafe_allow_html=True
                    )

    # def render_settings(self):
    #     """Render settings page"""
    #     st.subheader("⚙️ Settings")

    #     st.info("Settings are for display. Functionality to be added.")

    #     if st.button("Reset All Data"):
    #         response = self.api_client.reset_processed_emails()
    #         if response and response.get("success"):
    #             st.success("All data has been successfully reset!")
    #             # Also clear local state if needed
    #             st.session_state.websocket_data['latest_events'] = []
    #             st.rerun()
    #         else:
    #             st.error("Failed to reset data.")

    def render_dashboard(self):
        """Render the main dashboard"""
        
        st.markdown('<div ><h1>Email Support Demo</h1></div>', unsafe_allow_html=True)
        self.render_dashboard_status_bars()
        # Get stats from session state (updated by websockets) or API as fallback
        stats = st.session_state.websocket_data.get('stats', {})
        if not stats:
            api_stats = self.api_client.get_stats()
            if api_stats and 'processing_stats' in api_stats:
                stats = api_stats['processing_stats']
        
        # Display metrics
        if stats:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Processed", stats.get('total_processed', 0))
            with col2:
                st.metric("Successful Replies", stats.get('successful_replies', 0))
            with col3:
                st.metric("Manual Reviews", stats.get('manual_reviews', 0))
            with col4:
                st.metric("Errors", stats.get('errors', 0))
        else:
            st.info("No stats available.")

        # Charts and Events
        # st.markdown("---")
        col1, col2 = st.columns(2)
        
        with col1:
            # st.subheader("📊 Processing Distribution")
            if stats:
                labels = ['Successful', 'Manual Review', 'Errors']
                values = [
                    stats.get('successful_replies', 0),
                    stats.get('manual_reviews', 0),
                    stats.get('errors', 0)
                ]
                
                if any(values):
                    fig = px.pie(
                        values=values,
                        names=labels,
                        title="Email Processing Distribution",
                        hole=0.4,
                        color_discrete_sequence=['#28a745', '#ffc107', '#dc3545']
                    )
                    fig.update_layout(
                        paper_bgcolor='rgba(0,0,0,0)',
                        plot_bgcolor='rgba(0,0,0,0)',
                        font_color='white',
                        showlegend=False
                    )
                    fig.update_traces(textinfo='percent+label', textposition='inside')
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No data available to display charts.")
            else:
                st.info("No data available to display charts.")
        
        with col2:
            st.subheader("Recent Events")
            events = st.session_state.websocket_data.get('latest_events', [])
            if events:
                for i, event in enumerate(events[:5]):  # Show only last 5 events
                    with st.expander(f"Event {i+1}: {event.get('type', 'Unknown')}", expanded=False):
                        st.json(event)
            else:
                st.info("No recent events from WebSocket.")
    def render_dashboard_status_bars(self):
        """Render status bars for the dashboard"""
        
        # Get current status data
        is_processing = st.session_state.websocket_data.get('is_processing', False)
        connection_status = st.session_state.websocket_data.get('connection_status', 'Disconnected')
        last_update = st.session_state.websocket_data.get('last_update')
        stats = st.session_state.websocket_data.get('stats', {})
        
        # Create status bar container
        st.markdown('<div class="dashboard-status-container">', unsafe_allow_html=True)
        
        # Create columns for status cards
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            # Agent Processing Status
            if is_processing:
                status_class = "status-processing"
                status_text = "Processing Email"
                status_icon = "🔄"
            else:
                status_class = "status-running"
                status_text = "Ready"
                status_icon = "✅"
                
            st.markdown(f"""
                <div class="dashboard-status-card">
                    <div class="dashboard-status-icon {status_class}"></div>
                    <div class="dashboard-status-content">
                        <p class="dashboard-status-title">Agent Status</p>
                        <p class="dashboard-status-value">{status_icon} {status_text}</p>
                    </div>
                </div>
            """, unsafe_allow_html=True)
        
        with col2:
            # Connection Status
            if connection_status == 'Connected':
                conn_class = "status-running"
                conn_text = "Connected"
                conn_icon = "🔗"
            elif connection_status == 'Connecting...':
                conn_class = "status-warning"
                conn_text = "Connecting"
                conn_icon = "⏳"
            else:
                conn_class = "status-stopped"
                conn_text = "Disconnected"
                conn_icon = "❌"
                
            st.markdown(f"""
                <div class="dashboard-status-card">
                    <div class="dashboard-status-icon {conn_class}"></div>
                    <div class="dashboard-status-content">
                        <p class="dashboard-status-title">Connection</p>
                        <p class="dashboard-status-value">{conn_icon} {conn_text}</p>
                    </div>
                </div>
            """, unsafe_allow_html=True)
        
        with col3:
            # Last Activity
            if last_update:
                time_diff = datetime.now() - last_update
                if time_diff.total_seconds() < 60:
                    activity_text = "Just now"
                    activity_class = "status-running"
                elif time_diff.total_seconds() < 300:  # 5 minutes
                    activity_text = f"{int(time_diff.total_seconds()//60)}m ago"
                    activity_class = "status-warning"
                else:
                    activity_text = "Inactive"
                    activity_class = "status-stopped"
            else:
                activity_text = "No activity"
                activity_class = "status-stopped"
                
            st.markdown(f"""
                <div class="dashboard-status-card">
                    <div class="dashboard-status-icon {activity_class}"></div>
                    <div class="dashboard-status-content">
                        <p class="dashboard-status-title">Last Activity</p>
                        <p class="dashboard-status-value">⏰ {activity_text}</p>
                    </div>
                </div>
            """, unsafe_allow_html=True)
        
        with col4:
            # Queue Status
            manual_emails = self.api_client.get_manual_review_emails()
            queue_count = len(manual_emails) if manual_emails else 0
            
            if queue_count > 0:
                queue_class = "status-warning"
                queue_text = f"{queue_count} pending"
                queue_icon = "📋"
            else:
                queue_class = "status-running"
                queue_text = "Empty"
                queue_icon = "✅"
                
            st.markdown(f"""
                <div class="dashboard-status-card">
                    <div class="dashboard-status-icon {queue_class}"></div>
                    <div class="dashboard-status-content">
                        <p class="dashboard-status-title">Review Queue</p>
                        <p class="dashboard-status-value">{queue_icon} {queue_text}</p>
                    </div>
                </div>
            """, unsafe_allow_html=True)
        
        st.markdown('</div>', unsafe_allow_html=True)

# --- Main Application ---
def main():
    """Main function to run the Streamlit app."""
    
    # Create websocket manager as a singleton
    @st.cache_resource
    def get_websocket_manager():
        logger.info("Initializing and starting WebSocketManager.")
        manager = WebSocketManager(WEBSOCKET_URL)
        manager.start()
        return manager

    # Create instances
    api_client = APIClient(API_BASE_URL)
    ws_manager = get_websocket_manager()
    ui = UI(api_client)

    # Process WebSocket updates first
    def process_websocket_updates():
        """Process websocket updates and return if any updates occurred"""
        updates_processed = False
        
        while not ws_manager.queue.empty():
            message = ws_manager.queue.get()
            msg_type = message.get('type')
            updates_processed = True

            # Update session state
            if msg_type == 'connection_status':
                st.session_state.websocket_data['connection_status'] = message.get('status', 'Unknown')
                st.session_state.websocket_data['connected'] = (message.get('status') == 'Connected')
            
            elif msg_type == 'processing_started':
                st.session_state.websocket_data['is_processing'] = True

            elif msg_type == 'email_processed':
                st.session_state.websocket_data['is_processing'] = False
                st.session_state.websocket_data['last_processed_event'] = message
                st.session_state.websocket_data['latest_events'].insert(0, message)
                st.session_state.websocket_data['latest_events'] = st.session_state.websocket_data['latest_events'][:10]
                
                # Store email data
                final_record = message.get('record', {})
                result = message.get('result', {})
                
                ai_response = None
                if isinstance(result, dict):
                    final_state = result.get('final_state', {})
                    if isinstance(final_state, dict):
                        reply_response = final_state.get('reply_response', {})
                        if isinstance(reply_response, dict):
                            ai_response = reply_response.get('body')
                
                if final_record.get('message_id'):
                    final_record['ai_response'] = ai_response
                    st.session_state.processed_emails[final_record['message_id']] = final_record
                
                # Store toast data in session state
                st.session_state.pending_toast = {
                    'type': 'email_processed',
                    'message': message
                }

            # Update stats if present
            if 'stats' in message:
                st.session_state.websocket_data['stats'] = message['stats']

            st.session_state.websocket_data['last_update'] = datetime.now()

        return updates_processed

    # Process updates and trigger rerun if needed
    if process_websocket_updates():
        st.session_state.needs_rerun = True

    # Show pending toast after processing updates
    if st.session_state.pending_toast:
        toast_data = st.session_state.pending_toast
        st.session_state.pending_toast = None  # Clear it immediately
        
        if toast_data['type'] == 'email_processed':
            ui.show_toast_notification(toast_data['message'])

    # Render UI components
    ui.render_custom_css()
    page = ui.render_sidebar()

    # Render main content based on selected page
    if page == "Dashboard":
        ui.render_dashboard()
    elif page == "Manual Review":
        ui.render_manual_review()
    elif page == "History":
        ui.render_history()
    # elif page == "Settings":
    #     ui.render_settings()

    # Auto-refresh mechanism - trigger rerun if needed
    if st.session_state.needs_rerun:
        st.session_state.needs_rerun = False
        time.sleep(0.1)  # Small delay to prevent rapid reruns
        st.rerun()

    # Periodic check for updates (every 2 seconds)
    time.sleep(2)
    st.rerun()

if __name__ == "__main__":
    main()
