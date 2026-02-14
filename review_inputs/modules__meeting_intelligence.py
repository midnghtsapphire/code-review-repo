"""
Revvel Email Organizer - Meeting Intelligence Module
Integrates Fathom AI, calendar events, and email threads
"""
import logging
import httpx
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_
from config.settings import settings
from core.models import Meeting, Email
from core.llm import get_llm_client

logger = logging.getLogger(__name__)


class MeetingIntelligence:
    """Meeting intelligence and calendar integration"""
    
    def __init__(self, db: Session, calendar_service):
        self.db = db
        self.calendar_service = calendar_service
        self.llm = get_llm_client()
        self.fathom_api_key = settings.fathom_api_key
        self.fathom_url = settings.fathom_api_url
    
    async def sync_calendar_events(self, account_id: int) -> Dict:
        """Sync calendar events"""
        results = {'synced': 0, 'errors': 0}
        
        try:
            now = datetime.utcnow()
            time_min = now.isoformat() + 'Z'
            time_max = (now + timedelta(days=30)).isoformat() + 'Z'
            
            events = self.calendar_service.events().list(
                calendarId='primary',
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            for event in events.get('items', []):
                meeting = Meeting(
                    account_id=account_id,
                    calendar_id='primary',
                    event_id=event['id'],
                    title=event['summary'],
                    description=event.get('description'),
                    start_time=datetime.fromisoformat(event['start'].get('dateTime', event['start'].get('date')).replace('Z', '+00:00')),
                    end_time=datetime.fromisoformat(event['end'].get('dateTime', event['end'].get('date')).replace('Z', '+00:00')),
                    attendees=str([a['email'] for a in event.get('attendees', [])]),
                    location=event.get('location'),
                    meeting_link=event.get('conferenceData', {}).get('entryPoints', [{}])[0].get('uri'),
                )
                
                self.db.add(meeting)
                results['synced'] += 1
            
            self.db.commit()
            
        except Exception as e:
            logger.error(f"Error syncing calendar: {e}")
            results['errors'] += 1
        
        return results
    
    async def generate_meeting_prep(self, meeting_id: int) -> str:
        """Generate pre-meeting brief from email threads"""
        meeting = self.db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return ""
        
        try:
            attendees = eval(meeting.attendees) if isinstance(meeting.attendees, str) else []
            
            emails = self.db.query(Email).filter(
                Email.sender.in_(attendees)
            ).order_by(Email.received_at.desc()).limit(10).all()
            
            email_data = [
                {
                    'sender': e.sender,
                    'subject': e.subject,
                    'body': e.body[:500],
                    'date': e.received_at.isoformat()
                }
                for e in emails
            ]
            
            summary = await self.llm.summarize_thread(email_data)
            meeting.prep_summary = summary
            self.db.commit()
            
            return summary
        except Exception as e:
            logger.error(f"Error generating meeting prep: {e}")
            return ""
    
    async def import_fathom_transcript(self, meeting_id: int, fathom_recording_id: str) -> Optional[str]:
        """Import Fathom AI transcript"""
        if not self.fathom_api_key:
            logger.warning("Fathom API key not configured")
            return None
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.fathom_url}/v1/recordings/{fathom_recording_id}/transcript",
                    headers={"Authorization": f"Bearer {self.fathom_api_key}"},
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    data = response.json()
                    transcript = data.get('transcript', '')
                    
                    meeting = self.db.query(Meeting).filter(Meeting.id == meeting_id).first()
                    if meeting:
                        meeting.transcript = transcript
                        meeting.fathom_id = fathom_recording_id
                        self.db.commit()
                    
                    return transcript
                else:
                    logger.error(f"Fathom API error: {response.text}")
                    return None
        except Exception as e:
            logger.error(f"Error importing Fathom transcript: {e}")
            return None
    
    async def extract_action_items_from_meeting(self, meeting_id: int) -> List[str]:
        """Extract action items from meeting transcript"""
        meeting = self.db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting or not meeting.transcript:
            return []
        
        action_items = await self.llm.extract_action_items(meeting.transcript)
        meeting.action_items = str(action_items)
        self.db.commit()
        
        return action_items
    
    async def generate_followup_email(self, meeting_id: int) -> str:
        """Generate follow-up email after meeting"""
        meeting = self.db.query(Meeting).filter(Meeting.id == meeting_id).first()
        if not meeting:
            return ""
        
        prompt = f"""Generate a professional follow-up email after this meeting:

Title: {meeting.title}
Attendees: {meeting.attendees}
Summary: {meeting.summary or 'No summary available'}
Action Items: {meeting.action_items or 'None'}

Write a concise follow-up email."""
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{settings.openrouter_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openrouter_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.llm_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": 400,
                    },
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    return result["choices"][0]["message"]["content"]
                else:
                    return "Unable to generate follow-up email"
        except Exception as e:
            logger.error(f"Error generating follow-up: {e}")
            return "Unable to generate follow-up email"
