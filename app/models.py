from pydantic import BaseModel, Field, HttpUrl


class DownloadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    # Optional explicit Drive file id when resolving a folder member
    drive_file_id: str | None = Field(default=None, max_length=128)


class DriveResolveRequest(BaseModel):
    url: HttpUrl


class DriveAuthCodeRequest(BaseModel):
    # Accepts either a bare OAuth code or the full http://localhost/?code=... URL
    code: str = Field(min_length=5, max_length=2000)
