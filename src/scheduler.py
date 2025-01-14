import pandas as pd
import yaml
from datetime import datetime, timedelta
import time
import subprocess
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class SchedulerConfigError(Exception):
    """Raised when there are issues with scheduler configuration"""
    pass

class MediaScheduler:
    def __init__(self, config_path="config/scheduler_config.yml"):
        self.config_path = Path(config_path)
        self.validate_and_load_config()
        
    def validate_and_load_config(self):
        """Validate and load all configuration files"""
        # Check scheduler config exists
        if not self.config_path.exists():
            raise SchedulerConfigError(f"Scheduler config not found: {self.config_path}")
            
        # Load and validate scheduler config
        try:
            with open(self.config_path, 'r') as f:
                self.config = yaml.safe_load(f)
                
            # Validate required fields
            if 'schedule' not in self.config:
                raise SchedulerConfigError("Missing 'schedule' in config")
            if 'media_list' not in self.config:
                raise SchedulerConfigError("Missing 'media_list' in config")
                
            # Validate schedule entries
            for entry in self.config['schedule']:
                required_fields = ['time', 'window_hours', 'max_tasks']
                missing = [f for f in required_fields if f not in entry]
                if missing:
                    raise SchedulerConfigError(f"Schedule entry missing fields: {missing}")
                    
        except yaml.YAMLError as e:
            raise SchedulerConfigError(f"Invalid YAML in scheduler config: {e}")
            
        # Check and load media list
        media_list_path = Path(self.config['media_list'])
        if not media_list_path.exists():
            raise SchedulerConfigError(f"Media list not found: {media_list_path}")
            
        try:
            self.media_df = pd.read_csv(media_list_path)
            required_columns = ['file_path', 'caption']
            missing = [c for c in required_columns if c not in self.media_df.columns]
            if missing:
                raise SchedulerConfigError(f"Media list missing columns: {missing}")
                
            if '_PROCESSED' not in self.media_df.columns:
                self.media_df['_PROCESSED'] = False
                
        except pd.errors.EmptyDataError:
            raise SchedulerConfigError("Media list file is empty")
        except pd.errors.ParserError as e:
            raise SchedulerConfigError(f"Invalid CSV format in media list: {e}")
            
        # Sort schedule times for consistent ordering
        self.schedule_times = sorted([s['time'] for s in self.config['schedule']])
        self.schedule_config = {s['time']: s for s in self.config['schedule']}
        
        # Track tasks completed in current window
        self.current_window = None
        self.tasks_in_window = 0

    def update_media_list(self, new_path):
        """Update media list path and reload configuration"""
        self.config['media_list'] = str(new_path)
        self.validate_and_load_config()

    def get_next_schedule_time(self, from_time=None):
        """Calculate the next schedule time"""
        if from_time is None:
            from_time = datetime.now()
            
        current_time = from_time.strftime("%H:%M")
        
        # Find next time slot today
        next_today = next(
            (t for t in self.schedule_times if t > current_time),
            None
        )
        
        if next_today:
            next_time = datetime.combine(from_time.date(), 
                                       datetime.strptime(next_today, "%H:%M").time())
        else:
            # If no remaining slots today, get first slot tomorrow
            next_time = datetime.combine(from_time.date() + timedelta(days=1), 
                                       datetime.strptime(self.schedule_times[0], "%H:%M").time())
        
        return next_time

    def is_within_window(self, schedule_time):
        """Check if current time is within the window of a scheduled time"""
        now = datetime.now()
        window_config = self.schedule_config[schedule_time.strftime("%H:%M")]
        window_end = schedule_time + timedelta(hours=window_config['window_hours'])
        
        # Reset task counter if we've moved to a new window
        if self.current_window != schedule_time:
            self.current_window = schedule_time
            self.tasks_in_window = 0
            
        # Check if we're within window and haven't exceeded max tasks
        if schedule_time <= now <= window_end:
            max_tasks = window_config['max_tasks']
            if self.tasks_in_window < max_tasks:
                return True
            else:
                logger.info(f"Maximum tasks ({max_tasks}) already completed for this window")
        
        return False

    def get_next_unprocessed_media(self):
        """Get the next unprocessed media items if within posting window"""
        unprocessed = self.media_df[~self.media_df['_PROCESSED']]
        if unprocessed.empty:
            return None
            
        # Get next schedule time
        schedule_time = self.get_next_schedule_time()
        
        # Check if we're within the posting window
        if self.is_within_window(schedule_time):
            # Get first unprocessed item
            next_item = unprocessed.iloc[0]
            self.tasks_in_window += 1
            return next_item
            
        return None

    def mark_as_processed(self, media_path):
        """Mark a media item as processed"""
        idx = self.media_df[self.media_df['file_path'] == media_path].index
        self.media_df.loc[idx, '_PROCESSED'] = True
        self.media_df.to_csv(self.config['media_list'], index=False)

    def run_upload(self, media_item):
        """Run the upload script with the media item"""
        try:
            result = subprocess.run([
                'python', 'run.py',
                '-f', media_item['file_path'],
                '-c', media_item['caption']
            ], check=True)
            
            if result.returncode == 0:
                self.mark_as_processed(media_item['file_path'])
                logger.info(f"Successfully processed {media_item['file_path']}")
                logger.info(f"Tasks completed in current window: {self.tasks_in_window}")
                return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to process {media_item['file_path']}: {e}")
            # Decrement task counter if upload fails
            self.tasks_in_window -= 1
        return False

    def run(self):
        """Main scheduler loop"""
        while True:
            media_item = self.get_next_unprocessed_media()
            
            if media_item is not None:
                logger.info(f"Processing media: {media_item['file_path']}")
                self.run_upload(media_item)
            
            # Sleep for a minute before checking again
            time.sleep(60)

def main(config_path=None, media_list=None):
    """
    Run the scheduler with the specified configuration
    
    Args:
        config_path: Path to scheduler config YAML file
        media_list: Path to media list CSV file (overrides config)
        
    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    try:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
        scheduler = MediaScheduler(config_path=config_path or "config/scheduler_config.yml")
        if media_list:
            scheduler.update_media_list(media_list)
            
        scheduler.run()
        return 0
        
    except SchedulerConfigError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        logger.exception("Unexpected error occurred")
        return 1

if __name__ == "__main__":
    exit(main()) 