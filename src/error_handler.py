"""
Comprehensive error handling and logging utilities
"""

import logging
import traceback
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime
from enum import Enum

from rich.console import Console
from rich.panel import Panel
from rich.text import Text


class ErrorSeverity(Enum):
    """Error severity levels"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ErrorCategory(Enum):
    """Error categories for better organization"""
    FILE_IO = "file_io"
    EXIF_PROCESSING = "exif_processing"
    HEIC_CONVERSION = "heic_conversion"
    PERMISSION = "permission"
    VALIDATION = "validation"
    UNKNOWN = "unknown"


class ProcessingError:
    """Structured error representation"""

    def __init__(self,
                 message: str,
                 file_path: str = None,
                 category: ErrorCategory = ErrorCategory.UNKNOWN,
                 severity: ErrorSeverity = ErrorSeverity.MEDIUM,
                 exception: Exception = None,
                 context: Dict[str, Any] = None):
        """
        Initialize processing error.

        Args:
            message: Human-readable error message
            file_path: File path related to error (if applicable)
            category: Error category
            severity: Error severity level
            exception: Original exception (if any)
            context: Additional context information
        """
        self.message = message
        self.file_path = file_path
        self.category = category
        self.severity = severity
        self.exception = exception
        self.context = context or {}
        self.timestamp = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary representation"""
        return {
            'message': self.message,
            'file_path': self.file_path,
            'category': self.category.value,
            'severity': self.severity.value,
            'exception_type': type(self.exception).__name__ if self.exception else None,
            'exception_message': str(self.exception) if self.exception else None,
            'context': self.context,
            'timestamp': self.timestamp.isoformat()
        }

    def __str__(self) -> str:
        """String representation of error"""
        parts = [f"[{self.severity.value.upper()}] {self.message}"]
        if self.file_path:
            parts.append(f"File: {self.file_path}")
        if self.exception:
            parts.append(f"Exception: {self.exception}")
        return " | ".join(parts)


class ErrorHandler:
    """Central error handling and logging system"""

    def __init__(self, log_file: str = None):
        """
        Initialize error handler.

        Args:
            log_file: Optional log file path for persistent logging
        """
        self.errors: List[ProcessingError] = []
        self.log_file = log_file
        self.logger = logging.getLogger(__name__)
        self.console = Console()

        # Setup file logging if specified
        if log_file:
            self._setup_file_logging(log_file)

    def _setup_file_logging(self, log_file: str):
        """Setup file logging handler"""
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.DEBUG)

            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            file_handler.setFormatter(formatter)

            self.logger.addHandler(file_handler)
            self.logger.info(f"File logging enabled: {log_file}")

        except Exception as e:
            self.logger.warning(f"Could not setup file logging: {e}")

    def add_error(self,
                  message: str,
                  file_path: str = None,
                  category: ErrorCategory = ErrorCategory.UNKNOWN,
                  severity: ErrorSeverity = ErrorSeverity.MEDIUM,
                  exception: Exception = None,
                  context: Dict[str, Any] = None) -> ProcessingError:
        """
        Add a new error to the handler.

        Args:
            message: Human-readable error message
            file_path: File path related to error
            category: Error category
            severity: Error severity level
            exception: Original exception
            context: Additional context

        Returns:
            ProcessingError instance
        """
        error = ProcessingError(
            message=message,
            file_path=file_path,
            category=category,
            severity=severity,
            exception=exception,
            context=context
        )

        self.errors.append(error)

        # Log the error
        log_level = self._get_log_level(severity)
        self.logger.log(log_level, str(error))

        # Log exception details if present
        if exception and severity in [ErrorSeverity.HIGH, ErrorSeverity.CRITICAL]:
            self.logger.debug(f"Exception traceback: {traceback.format_exc()}")

        return error

    def _get_log_level(self, severity: ErrorSeverity) -> int:
        """Map error severity to logging level"""
        mapping = {
            ErrorSeverity.LOW: logging.INFO,
            ErrorSeverity.MEDIUM: logging.WARNING,
            ErrorSeverity.HIGH: logging.ERROR,
            ErrorSeverity.CRITICAL: logging.CRITICAL
        }
        return mapping.get(severity, logging.WARNING)

    def handle_file_error(self, file_path: str, operation: str, exception: Exception) -> ProcessingError:
        """
        Handle file-related errors with automatic categorization.

        Args:
            file_path: File path that caused the error
            operation: Operation being performed
            exception: Exception that occurred

        Returns:
            ProcessingError instance
        """
        # Categorize the error based on exception type
        if isinstance(exception, PermissionError):
            category = ErrorCategory.PERMISSION
            severity = ErrorSeverity.HIGH
        elif isinstance(exception, FileNotFoundError):
            category = ErrorCategory.FILE_IO
            severity = ErrorSeverity.MEDIUM
        elif isinstance(exception, OSError):
            category = ErrorCategory.FILE_IO
            severity = ErrorSeverity.MEDIUM
        else:
            category = ErrorCategory.UNKNOWN
            severity = ErrorSeverity.MEDIUM

        message = f"Failed to {operation}: {file_path}"

        return self.add_error(
            message=message,
            file_path=file_path,
            category=category,
            severity=severity,
            exception=exception,
            context={'operation': operation}
        )

    def handle_heic_error(self, file_path: str, exception: Exception) -> ProcessingError:
        """Handle HEIC conversion errors"""
        return self.add_error(
            message=f"HEIC conversion failed: {file_path}",
            file_path=file_path,
            category=ErrorCategory.HEIC_CONVERSION,
            severity=ErrorSeverity.MEDIUM,
            exception=exception,
            context={'conversion_type': 'heic_to_jpeg'}
        )

    def handle_exif_error(self, file_path: str, exception: Exception) -> ProcessingError:
        """Handle EXIF processing errors"""
        return self.add_error(
            message=f"EXIF processing failed: {file_path}",
            file_path=file_path,
            category=ErrorCategory.EXIF_PROCESSING,
            severity=ErrorSeverity.LOW,  # Usually not critical
            exception=exception,
            context={'exif_operation': 'timestamp_extraction'}
        )

    def get_error_summary(self) -> Dict[str, Any]:
        """
        Get comprehensive error summary.

        Returns:
            Dictionary with error statistics and breakdown
        """
        if not self.errors:
            return {'total_errors': 0}

        # Count by severity
        severity_counts = {}
        for severity in ErrorSeverity:
            severity_counts[severity.value] = sum(
                1 for error in self.errors if error.severity == severity
            )

        # Count by category
        category_counts = {}
        for category in ErrorCategory:
            category_counts[category.value] = sum(
                1 for error in self.errors if error.category == category
            )

        # Get unique affected files
        affected_files = set(
            error.file_path for error in self.errors if error.file_path
        )

        return {
            'total_errors': len(self.errors),
            'severity_breakdown': severity_counts,
            'category_breakdown': category_counts,
            'affected_files_count': len(affected_files),
            'affected_files': list(affected_files),
            'critical_errors': [
                error.to_dict() for error in self.errors
                if error.severity == ErrorSeverity.CRITICAL
            ],
            'recent_errors': [
                error.to_dict() for error in self.errors[-5:]  # Last 5 errors
            ]
        }

    def display_error_summary(self):
        """Display error summary using rich formatting"""
        if not self.errors:
            self.console.print("[green]No errors encountered![/green]")
            return

        summary = self.get_error_summary()

        # Create error summary text
        error_text = Text(f"Encountered {summary['total_errors']} errors during processing", style="bold yellow")

        # Add severity breakdown
        error_text.append("\n\nBy Severity:", style="bold")
        for severity, count in summary['severity_breakdown'].items():
            if count > 0:
                style = self._get_severity_style(severity)
                error_text.append(f"\n  {severity.title()}: {count}", style=style)

        # Add category breakdown
        error_text.append("\n\nBy Category:", style="bold")
        for category, count in summary['category_breakdown'].items():
            if count > 0:
                error_text.append(f"\n  {category.replace('_', ' ').title()}: {count}", style="dim")

        # Add affected files info
        if summary['affected_files_count'] > 0:
            error_text.append(f"\n\nAffected Files: {summary['affected_files_count']}", style="bold")

        panel = Panel(
            error_text,
            title="Error Summary",
            border_style="yellow",
            padding=(1, 2)
        )
        self.console.print(panel)

        # Display critical errors if any
        critical_errors = summary.get('critical_errors', [])
        if critical_errors:
            self.console.print("\n[bold red]Critical Errors:[/bold red]")
            for error in critical_errors:
                self.console.print(f"  • {error['message']}", style="red")

    def _get_severity_style(self, severity: str) -> str:
        """Get rich style for error severity"""
        styles = {
            'low': 'dim',
            'medium': 'yellow',
            'high': 'red',
            'critical': 'bold red'
        }
        return styles.get(severity, 'white')

    def export_error_log(self, output_file: str) -> bool:
        """
        Export error log to JSON file.

        Args:
            output_file: Output file path

        Returns:
            True if successful, False otherwise
        """
        try:
            import json

            error_data = {
                'export_timestamp': datetime.now().isoformat(),
                'summary': self.get_error_summary(),
                'errors': [error.to_dict() for error in self.errors]
            }

            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, 'w') as f:
                json.dump(error_data, f, indent=2, default=str)

            self.logger.info(f"Error log exported to: {output_file}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to export error log: {e}")
            return False

    def has_critical_errors(self) -> bool:
        """Check if any critical errors occurred"""
        return any(error.severity == ErrorSeverity.CRITICAL for error in self.errors)

    def has_errors(self) -> bool:
        """Check if any errors occurred"""
        return len(self.errors) > 0

    def clear_errors(self):
        """Clear all errors"""
        self.errors.clear()
        self.logger.info("Error log cleared")