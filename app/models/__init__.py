"""ORM models. Importing this package ensures all models are registered
on `Base.metadata` for Alembic autogenerate."""

from app.models.base import Base
from app.models.cv import (
    CvCharacter,
    CvCharacterVolume,
    CvIssue,
    CvPerson,
    CvPublisher,
    CvSearchCache,
    CvStoryArc,
    CvTeam,
    CvVolume,
)
from app.models.file import (
    ArchiveFormat,
    ComicInfoStatus,
    File,
    FileError,
    FileErrorKind,
    FileLocation,
)
from app.models.file_match import FileMatch, MatchSource, MatchStatus
from app.models.local import LocalIssue, LocalVolume
from app.models.read_progress import ReadProgress
from app.models.settings import AppSetting
from app.models.user import User, UserRole

__all__ = [
    "AppSetting",
    "ArchiveFormat",
    "Base",
    "ComicInfoStatus",
    "CvCharacter",
    "CvCharacterVolume",
    "CvIssue",
    "CvPerson",
    "CvPublisher",
    "CvSearchCache",
    "CvStoryArc",
    "CvTeam",
    "CvVolume",
    "File",
    "FileError",
    "FileErrorKind",
    "FileLocation",
    "FileMatch",
    "LocalIssue",
    "LocalVolume",
    "MatchSource",
    "MatchStatus",
    "ReadProgress",
    "User",
    "UserRole",
]
