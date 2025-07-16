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

API_BASE_URL = "http://18.61.69.3:5000"
WEBSOCKET_URL = "ws://18.61.69.3:5000/ws"


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
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API request to {url} failed: {e}")
            st.error(f"Error communicating with the API. Please ensure the backend is running.")
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
        return self._request("post", "/send-manual-reply", json=data) or \
            {"success": False, "message": "API request failed"}

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

    def process_updates(self) -> bool:
        """Process all messages from the WebSocket queue."""
        if self.queue.empty():
            return False
            
        updates_processed = False
        with self.lock:
            while not self.queue.empty():
                message = self.queue.get()
                updates_processed = True
                msg_type = message.get('type')

                if msg_type == 'connection_status':
                    st.session_state.websocket_data['connection_status'] = message.get('status', 'Unknown')
                    st.session_state.websocket_data['connected'] = (message.get('status') == 'Connected')
                elif msg_type == 'processing_started':
                    st.session_state.websocket_data['is_processing'] = True
                elif msg_type == 'status_update':
                    st.session_state.websocket_data['status'] = message
                elif msg_type == 'email_processed':
                    st.session_state.websocket_data['is_processing'] = False
                    st.session_state.websocket_data['last_processed_event'] = message
                    st.session_state.websocket_data['latest_events'].insert(0, message)
                    # Keep only the last 10 events
                    st.session_state.websocket_data['latest_events'] = st.session_state.websocket_data['latest_events'][:10]

                # Update stats if present
                if msg_type in ['email_processed', 'status_update', 'processing_started']:
                    st.session_state.should_rerun = True
                    
                st.session_state.websocket_data['last_update'] = datetime.now()
        
        return updates_processed

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
            </style>
        """, unsafe_allow_html=True)

    def render_sidebar(self) -> str:
        """Render the sidebar with navigation and status"""
        with st.sidebar:
            st.markdown('<div class="sidebar-header"><h1>📧 Email Support Agent</h1></div>', unsafe_allow_html=True)
            
            page = st.radio("Navigation", ["Dashboard", "Manual Review", "History", "Settings"], key="navigation")

            st.markdown("<br>", unsafe_allow_html=True)
            st.subheader("Agent Status")

            # Create placeholders for dynamic status updates
            self.processing_status_placeholder = st.empty()
            self.connection_status_placeholder = st.empty()

            # Display initial state
            self.update_processing_status(st.session_state.websocket_data.get('is_processing'))
            self.update_connection_status(st.session_state.websocket_data.get('connection_status'))

            # st.markdown("---")
            # st.subheader("Quick Stats")
            # # Create placeholder for quick stats
            # self.quick_stats_placeholder = st.empty()
            # Get initial stats from API
            # initial_stats = self.api_client.get_stats()
            # self.update_quick_stats(initial_stats.get('processing_stats', {}))

            st.markdown("---")
            st.info("Navigate to other pages using the options above.")

        return page

    def render_recent_events(self):
        """Render recent events from WebSocket"""
        st.subheader("Recent Events")
        
        events = st.session_state.websocket_data.get('latest_events', [])
        if not events:
            st.info("No recent events from WebSocket.")
        else:
            for i, event in enumerate(events[:5]):  # Show only last 5 events
                with st.expander(f"Event {i+1}: {event.get('type', 'Unknown')}", expanded=False):
                    st.json(event)

    def update_connection_status(self, status):
        """Update connection status in the sidebar"""
        self.connection_status_placeholder.empty()
        if status == 'Connected':
            self.connection_status_placeholder.markdown(f'<p class="status-badge status-connected">🟢 {status}</p>', unsafe_allow_html=True)
        else:
            self.connection_status_placeholder.markdown(f'<p class="status-badge status-disconnected">🔴 {status}</p>', unsafe_allow_html=True)

    def update_processing_status(self, is_processing):
        """Update processing status in the sidebar"""
        with self.processing_status_placeholder.container():
            if is_processing:
                st.spinner("Processing...")

    def update_metrics(self, stats):
        """Update metrics in the dashboard"""
        with self.metrics_placeholder.container():
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

    def update_recent_events(self, events):
        """Update recent events in the dashboard"""
        with self.events_placeholder.container():
            st.subheader("Recent Events")
            if events:
                for i, event in enumerate(events[:5]):  # Show only last 5 events
                    with st.expander(f"Event {i+1}: {event.get('type', 'Unknown')}", expanded=False):
                        st.json(event)
            else:
                st.info("No recent events from WebSocket.")

    def update_charts(self, stats):
        """Update charts in the dashboard"""
        self.charts_placeholder.empty()
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
                self.charts_placeholder.plotly_chart(fig, use_container_width=True)
            else:
                self.charts_placeholder.info("No data available to display charts.")

    # def update_quick_stats(self, stats):
    #     """Update the quick stats in the sidebar."""
    #     with self.quick_stats_placeholder.container():
    #         if stats:
    #             st.markdown(f"- **Total Processed:** {stats.get('total_processed', 0)}")
    #             st.markdown(f"- **Successful Replies:** {stats.get('successful_replies', 0)}")
    #             st.markdown(f"- **Manual Reviews:** {stats.get('manual_reviews', 0)}")
    #             st.markdown(f"- **Errors:** {stats.get('errors', 0)}")
    #         else:
    #             st.info("No stats available.")

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
        st.markdown('<div class="main-header"><h1>📝 Manual Review Queue</h1></div>', unsafe_allow_html=True)
        
        # Get manual review emails
        manual_emails = self.api_client.get_manual_review_emails()
        
        if not manual_emails:
            st.info("🎉 No emails currently require manual review.")
            return
        
        st.info(f"📬 {len(manual_emails)} emails require manual review")
        
        # Display emails
        for i, email in enumerate(manual_emails):
            priority = email.get('priority', 'low')
            priority_class = f"priority-{priority}" if priority in ['high', 'medium'] else ""
            
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
                    key=f"content_{i}"
                
                
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
                    col1, col2, col3 = st.columns([1, 1, 2])
                    
                    with col1:
                        send_reply = st.form_submit_button("📤 Send Reply", type="primary")
                    
                    with col2:
                        save_draft = st.form_submit_button("💾 Save Draft")
                    
                    with col3:
                        mark_resolved = st.form_submit_button("✅ Mark as Resolved")
                    
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
                                
                                if result.get("success"):
                                    st.success("✅ Reply sent successfully!")
                                    st.balloons()
                                    time.sleep(2)
                                    st.rerun()
                                else:
                                    st.error(f"❌ Failed to send reply: {result.get('message', 'Unknown error')}")
                        else:
                            st.error("❌ Please enter a reply message.")
                    
                    if save_draft:
                        st.info("💾 Draft saved (feature not implemented)")
                    
                    if mark_resolved:
                        st.success("✅ Email marked as resolved (feature not implemented)")

    def render_history(self):
        """Render email history from session state."""
        st.markdown('<div class="main-header"><h1>📜 Email History</h1></div>', unsafe_allow_html=True)

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
                # 'Response Sent': 'Yes' if email.get('response_sent') else 'No',
                # 'Manual Review': 'Yes' if email.get('requires_manual_review') else 'No'
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
                    # st.markdown(f"**Message ID:** `{selected_mail.get('message_id', '')}`")
                    st.markdown(f"**Timestamp:** `{selected_mail.get('timestamp', '')}`")
                    # st.markdown(f"**Response Sent:** `{'Yes' if selected_mail.get('response_sent') else 'No'}`")

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

    def render_settings(self):
        """Render settings page"""
        st.subheader("⚙️ Settings")

        st.info("Settings are for display. Functionality to be added.")

        if st.button("Reset All Data"):
            response = self.api_client.reset_processed_emails()
            if response and response.get("success"):
                st.success("All data has been successfully reset!")
                # Also clear local state if needed
                st.session_state.websocket_data['latest_events'] = []
                st.rerun()
            else:
                st.error("Failed to reset data.")

    def render_dashboard(self):
        """Render the main dashboard"""
        st.markdown('<div class="main-header"><h1>📊 Email Support Demo</h1></div>', unsafe_allow_html=True)
        
        # Create placeholders for metrics
        self.metrics_placeholder = st.empty()

        # Get initial stats from API as a fallback
        p_stats = {d: 0 for d in ['total_processed', 'successful_replies', 'manual_reviews', 'errors']}
        api_stats = self.api_client.get_stats()
        if api_stats and 'processing_stats' in api_stats:
            p_stats = api_stats['processing_stats']
        
        self.update_metrics(p_stats)

        # Charts and Events placeholders
        st.markdown("---")
        col1, col2 = st.columns(2)
        
        with col1:
            self.charts_placeholder = st.empty()
            self.update_charts(p_stats)
        
        with col2:
            self.events_placeholder = st.empty()
            self.update_recent_events(st.session_state.websocket_data.get('latest_events', []))

# --- Main Application ---
def main():
    """Main function to run the Streamlit app."""
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

    # Render static UI components once
    ui.render_custom_css()
    page = ui.render_sidebar() # This now also creates placeholders in the sidebar

    # Render main content layout based on selected page
    if page == "Dashboard":
        ui.render_dashboard() # This now also creates placeholders in the main area
    elif page == "Manual Review":
        ui.render_manual_review()
    elif page == "History":
        ui.render_history()
    elif page == "Settings":
        ui.render_settings()

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
                ui.update_connection_status(st.session_state.websocket_data['connection_status'])
            
            elif msg_type == 'processing_started':
                st.session_state.websocket_data['is_processing'] = True
                ui.update_processing_status(True)

            elif msg_type == 'email_processed':
                st.session_state.websocket_data['is_processing'] = False
                st.session_state.websocket_data['last_processed_event'] = message
                st.session_state.websocket_data['latest_events'].insert(0, message)
                st.session_state.websocket_data['latest_events'] = st.session_state.websocket_data['latest_events'][:10]
                
                # Store email data
                final_record = message.get('record', {})
                result = message.get('result', {})
                
                ai_response = None
                if result.get('final_state', {}).get('reply_response', {}).get('body'):
                    ai_response = result['final_state']['reply_response']['body']
                
                if final_record.get('message_id'):
                    final_record['ai_response'] = ai_response
                    st.session_state.processed_emails[final_record['message_id']] = final_record
                
                ui.update_processing_status(False)
                ui.update_recent_events(st.session_state.websocket_data['latest_events'])
                
                # Store toast data in session state instead of showing immediately
                st.session_state.pending_toast = {
                    'type': 'email_processed',
                    'message': message
                }

            # Update stats if present
            if 'stats' in message:
                st.session_state.websocket_data['stats'] = message['stats']
                stats_data = message['stats'].get('processing_stats', {})
                ui.update_metrics(stats_data)
                if page == "Dashboard":
                    ui.update_charts(stats_data)

        return updates_processed

    # Add this right after the process_websocket_updates() call in main():
    # Process updates once
    if process_websocket_updates():
        st.rerun()

    # Show pending toast after rerun (add this right after the st.rerun() call)
    if st.session_state.pending_toast:
        toast_data = st.session_state.pending_toast
        st.session_state.pending_toast = None  # Clear it immediately
        
        if toast_data['type'] == 'email_processed':
            ui.show_toast_notification(toast_data['message'])

    time.sleep(0.1)

if __name__ == "__main__":
    main()
