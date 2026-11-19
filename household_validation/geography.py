"""Shared filtering of each candidate record's own location."""
from django.db.models import Q
from location.models import Hotspot, MicroCatchment


class ValidationGeographyMixin:
    def _apply_location_filters(
        self,
        queryset,
        region_id=None,
        region_code=None,
        district_id=None,
        district_code=None,
        ta_id=None,
        ta_code=None,
        ta_codes=None,
        gvh_codes=None,
        village_id=None,
        village_code=None,
        village_codes=None,
        hotspot_id=None,
        hotspot_code=None,
        catchment_id=None,
        catchment_code=None,
    ):
        if catchment_id or catchment_code:
            queryset = self._apply_micro_catchment_scope(
                queryset,
                catchment_id=catchment_id,
                catchment_code=catchment_code,
            )

        # Accept both the legacy singular ``village_code`` and the plural
        # ``village_codes`` list; merge them into a single code set.
        codes = list(village_codes or [])
        if village_code:
            codes.append(village_code)

        if village_id or codes:
            village_filter = Q()
            if village_id:
                village_filter |= Q(location_id=village_id)
            if codes:
                village_filter |= Q(location__code__in=codes)
            return queryset.filter(village_filter)

        selected_gvh_codes = list(dict.fromkeys(gvh_codes or []))
        if selected_gvh_codes:
            return queryset.filter(
                Q(location__code__in=selected_gvh_codes)
                | Q(location__parent__code__in=selected_gvh_codes)
            )

        # A hotspot is a curated set of specific villages, finer-grained than a
        # plain TA/GVH pick, so it's checked before those but still yields to an
        # explicit village/GVH selection made on top of it in the UI.
        if hotspot_id or hotspot_code:
            hotspot = self._resolve_hotspot(hotspot_id, hotspot_code)
            if hotspot is None:
                return queryset.none()
            hotspot_village_codes = list(hotspot.villages.values_list("code", flat=True))
            if not hotspot_village_codes:
                return queryset.none()
            return queryset.filter(location__code__in=hotspot_village_codes)

        selected_ta_codes = list(dict.fromkeys(ta_codes or []))
        if ta_code:
            selected_ta_codes.append(ta_code)

        if ta_id or selected_ta_codes:
            ta_filter = Q()
            if ta_id:
                ta_filter |= (
                    Q(location_id=ta_id)
                    | Q(location__parent_id=ta_id)
                    | Q(location__parent__parent_id=ta_id)
                )
            if selected_ta_codes:
                ta_filter |= (
                    Q(location__code__in=selected_ta_codes)
                    | Q(location__parent__code__in=selected_ta_codes)
                    | Q(location__parent__parent__code__in=selected_ta_codes)
                )
            return queryset.filter(ta_filter)

        if district_id or district_code:
            district_filter = Q()
            if district_id:
                district_filter |= (
                    Q(location_id=district_id)
                    | Q(location__parent_id=district_id)
                    | Q(location__parent__parent_id=district_id)
                    | Q(location__parent__parent__parent_id=district_id)
                )
            if district_code:
                district_filter |= (
                    Q(location__code=district_code)
                    | Q(location__parent__code=district_code)
                    | Q(location__parent__parent__code=district_code)
                    | Q(location__parent__parent__parent__code=district_code)
                )
            return queryset.filter(district_filter)
        if region_id or region_code:
            region_filter = Q()
            if region_id:
                region_filter |= (
                    Q(location_id=region_id)
                    | Q(location__parent_id=region_id)
                    | Q(location__parent__parent_id=region_id)
                    | Q(location__parent__parent__parent_id=region_id)
                )
            if region_code:
                region_filter |= (
                    Q(location__code=region_code)
                    | Q(location__parent__code=region_code)
                    | Q(location__parent__parent__code=region_code)
                    | Q(location__parent__parent__parent__code=region_code)
                )
            return queryset.filter(region_filter)
        return queryset

    def _apply_micro_catchment_scope(
        self,
        queryset,
        catchment_id=None,
        catchment_code=None,
    ):
        micro_catchment = self._resolve_micro_catchment(catchment_id, catchment_code)
        if micro_catchment is None:
            return queryset.none()
        location_filter = self._micro_catchment_location_filter(micro_catchment)
        if location_filter is None:
            return queryset.none()
        return queryset.filter(location_filter)

    def _micro_catchment_location_filter(self, micro_catchment):
        """Return the most specific active location coverage for a catchment."""
        village_codes = list(
            Hotspot.objects.filter(
                micro_catchment=micro_catchment,
                validity_to__isnull=True,
                village_links__validity_to__isnull=True,
                village_links__location__validity_to__isnull=True,
            )
            .values_list("village_links__location__code", flat=True)
            .distinct()
        )
        if village_codes:
            return Q(location__code__in=village_codes)

        gvh_codes = list(
            micro_catchment.gvhs.filter(
                validity_to__isnull=True,
                location__validity_to__isnull=True,
            ).values_list("location__code", flat=True)
        )
        if gvh_codes:
            return (
                Q(location__code__in=gvh_codes)
                | Q(location__parent__code__in=gvh_codes)
            )

        ta_codes = list(
            micro_catchment.traditional_authorities.filter(
                validity_to__isnull=True,
                location__validity_to__isnull=True,
            ).values_list("location__code", flat=True)
        )
        if ta_codes:
            return (
                Q(location__code__in=ta_codes)
                | Q(location__parent__code__in=ta_codes)
                | Q(location__parent__parent__code__in=ta_codes)
            )
        return None

    def _resolve_hotspot(self, hotspot_id, hotspot_code):
        identity_filter = Q()
        if hotspot_id:
            identity_filter |= Q(uuid=hotspot_id)
        if hotspot_code:
            identity_filter |= Q(code=hotspot_code)
        if not identity_filter:
            return None
        return Hotspot.objects.filter(identity_filter, validity_to__isnull=True).first()

    def _resolve_micro_catchment(self, catchment_id, catchment_code):
        identity_filter = Q()
        if catchment_id:
            identity_filter |= Q(uuid=catchment_id)
        if catchment_code:
            identity_filter |= Q(code=catchment_code)
        if not identity_filter:
            return None
        return MicroCatchment.objects.filter(identity_filter, validity_to__isnull=True).first()
