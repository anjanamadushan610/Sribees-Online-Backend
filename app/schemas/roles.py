"""
Role / Permission / Staff Schemas

Backs the delegated staff system (see app/api/v1/admin_roles.py,
app/api/v1/admin_staff.py, app/services/permission_service.py).
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


class PermissionOut(BaseModel):
    permission_id: UUID = Field(..., alias="permissionId")
    resource: str
    action: str

    class Config:
        from_attributes = True
        populate_by_name = True


class PermissionGrant(BaseModel):
    """A bare (resource, action) pair with no id — used where only the
    membership check matters (e.g. embedded on AdminResponse), not the
    catalog identity (e.g. the permission picker, which needs PermissionOut)."""
    resource: str
    action: str


class PermissionCatalogResponse(BaseModel):
    """The full delegatable catalog, plus the caller's own effective set so
    the frontend picker can grey out anything the caller doesn't hold."""
    success: bool = True
    permissions: list[PermissionOut]
    my_permission_ids: list[UUID] = Field(..., alias="myPermissionIds")

    class Config:
        populate_by_name = True


class RoleOut(BaseModel):
    role_id: UUID = Field(..., alias="roleId")
    name: str
    is_system: bool = Field(..., alias="isSystem")
    permissions: list[PermissionOut] = []

    class Config:
        from_attributes = True
        populate_by_name = True


class RoleListResponse(BaseModel):
    success: bool = True
    roles: list[RoleOut]


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    permission_ids: list[UUID] = Field(..., min_length=1, alias="permissionIds")

    class Config:
        populate_by_name = True


class RoleResponse(BaseModel):
    success: bool = True
    role: RoleOut


class CreateStaffRequest(BaseModel):
    """Exactly one of role_id (reuse an existing custom role this admin
    created) or new_role (define one inline) must be supplied."""
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=2, max_length=100, alias="fullName")
    branch_id: Optional[UUID] = Field(None, alias="branchId")
    role_id: Optional[UUID] = Field(None, alias="roleId")
    new_role: Optional[CreateRoleRequest] = Field(None, alias="newRole")

    class Config:
        populate_by_name = True

    @model_validator(mode="after")
    def _exactly_one_role_source(self) -> "CreateStaffRequest":
        if (self.role_id is None) == (self.new_role is None):
            raise ValueError("Provide exactly one of role_id or new_role.")
        return self


class UpdateStaffRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=100, alias="fullName")
    password: Optional[str] = Field(None, min_length=8)
    branch_id: Optional[UUID] = Field(None, alias="branchId")
    is_active: Optional[bool] = Field(None, alias="isActive")
    permission_ids: Optional[list[UUID]] = Field(None, alias="permissionIds")

    class Config:
        populate_by_name = True


class StaffOut(BaseModel):
    admin_id: UUID = Field(..., alias="adminId")
    email: EmailStr
    full_name: str = Field(..., alias="fullName")
    role: RoleOut
    branch_id: Optional[UUID] = Field(None, alias="branchId")
    branch_name: Optional[str] = Field(None, alias="branchName")
    parent_admin_id: UUID = Field(..., alias="parentAdminId")
    is_active: bool = Field(..., alias="isActive")
    created_at: datetime = Field(..., alias="createdAt")

    class Config:
        from_attributes = True
        populate_by_name = True


class StaffResponse(BaseModel):
    success: bool = True
    staff: StaffOut
    message: Optional[str] = None


class StaffListResponse(BaseModel):
    success: bool = True
    staff: list[StaffOut]
    total: int
