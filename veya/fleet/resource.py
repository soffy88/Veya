"""Fleet Resource Pools and Reservations (spec §12-14, §20, §40, §58).

Invariants:
- RESOURCE_OVERCOMMIT=0: Total allocated + reserved never exceeds pool capacity.
- RESOURCE_CROSS_TENANT_LEAK=0: Reservations are strictly tied to principal and mission.
- RESOURCE_RESERVATION_LEAK=0: Reservations are released on completion, failure, migration or expiration.
- Three-tier backpressure tracks fleet, runtime, and execution capacity.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .models import (
    ReservationStatus,
    ResourcePool,
    ResourceReservation,
    ResourceType,
)
from .registry import FleetRegistry


class ResourceExhaustedError(RuntimeError):
    """Raised when resource pool does not have enough capacity."""


class ResourceManager:
    """Manages resource pools, reservations, quotas, and saturation backpressure."""

    def __init__(self, registry: FleetRegistry):
        self.registry = registry
        self._lock = threading.RLock()

    def register_pool(
        self,
        pool_id: str,
        resource_type: ResourceType,
        capacity: float,
        metadata: dict[str, Any] | None = None,
    ) -> ResourcePool:
        with self._lock:
            existing = self.registry.get_pool(pool_id)
            if existing:
                return existing
            pool = ResourcePool(
                pool_id=pool_id,
                resource_type=resource_type,
                capacity=capacity,
                available=capacity,
                metadata=metadata or {},
            )
            self.registry.save_pool(pool)
            return pool

    def reserve(
        self,
        mission_id: str,
        pool_id: str,
        quantity: float,
        agent_instance_id: str = "",
        ttl_s: float = 3600.0,
    ) -> ResourceReservation:
        """Atomically reserve resources from a pool with overcommit prevention (spec §13, §14)."""
        with self._lock:
            pool = self.registry.get_pool(pool_id)
            if not pool:
                raise KeyError(f"Resource pool '{pool_id}' not found")

            # Check capacity availability
            if pool.available < quantity:
                raise ResourceExhaustedError(
                    f"Pool '{pool_id}' has available {pool.available} < requested {quantity}"
                )

            # Atomically update pool
            pool.available -= quantity
            pool.reserved += quantity
            self.registry.save_pool(pool)

            fleet = self.registry.get_fleet()
            res_id = f"res_{uuid.uuid4().hex[:12]}"
            reservation = ResourceReservation(
                reservation_id=res_id,
                mission_id=mission_id,
                agent_instance_id=agent_instance_id,
                pool_id=pool_id,
                quantity=quantity,
                created_at=time.time(),
                expires_at=time.time() + ttl_s,
                status=ReservationStatus.RESERVED,
            )
            self.registry.save_reservation(reservation, expected_fleet_generation=fleet.generation)
            return reservation

    def activate_reservation(self, reservation_id: str) -> None:
        """Transition reservation from RESERVED to ACTIVE."""
        with self._lock:
            res = self.registry.get_reservation(reservation_id)
            if not res or res.status != ReservationStatus.RESERVED:
                return
            pool = self.registry.get_pool(res.pool_id)
            if pool:
                pool.reserved -= res.quantity
                pool.allocated += res.quantity
                self.registry.save_pool(pool)

            res.status = ReservationStatus.ACTIVE
            self.registry.save_reservation(res)

    def release_reservation(self, reservation_id: str) -> None:
        """Release reservation and return capacity to pool (spec §13, RESOURCE_RESERVATION_LEAK=0)."""
        with self._lock:
            res = self.registry.get_reservation(reservation_id)
            if not res or res.status in (ReservationStatus.RELEASED, ReservationStatus.EXPIRED):
                return

            pool = self.registry.get_pool(res.pool_id)
            if pool:
                if res.status == ReservationStatus.RESERVED:
                    pool.reserved = max(0.0, pool.reserved - res.quantity)
                elif res.status == ReservationStatus.ACTIVE:
                    pool.allocated = max(0.0, pool.allocated - res.quantity)
                pool.available = min(pool.capacity, pool.available + res.quantity)
                self.registry.save_pool(pool)

            res.status = ReservationStatus.RELEASED
            self.registry.save_reservation(res)

    def release_all_for_mission(self, mission_id: str) -> int:
        """Release all active or reserved reservations for a mission."""
        with self._lock:
            count = 0
            for r in self.registry.list_reservations():
                if r.mission_id == mission_id and r.status in (
                    ReservationStatus.RESERVED,
                    ReservationStatus.ACTIVE,
                ):
                    self.release_reservation(r.reservation_id)
                    count += 1
            return count

    def reconcile_expired(self) -> int:
        """Reconcile and release expired reservations."""
        with self._lock:
            now = time.time()
            expired_count = 0
            for r in self.registry.list_reservations():
                if (
                    r.status in (ReservationStatus.RESERVED, ReservationStatus.ACTIVE)
                    and now > r.expires_at
                ):
                    self.release_reservation(r.reservation_id)
                    r.status = ReservationStatus.EXPIRED
                    self.registry.save_reservation(r)
                    expired_count += 1
            return expired_count

    def get_saturation(self) -> float:
        """Calculate overall fleet resource saturation ratio (spec §20, §46)."""
        with self._lock:
            pools = self.registry.list_pools()
            if not pools:
                return 0.0
            total_cap = sum(p.capacity for p in pools)
            if total_cap <= 0:
                return 0.0
            total_used = sum(p.allocated + p.reserved for p in pools)
            return min(1.0, max(0.0, total_used / total_cap))
