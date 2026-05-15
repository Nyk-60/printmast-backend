from django.db import transaction
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BalanceTransaction, PrintCenterProfile, SubUserProfile
from .permissions import IsPrintCenter, IsSuperAdmin
from .serializers import (
    AddBalanceSerializer,
    BalanceTransactionSerializer,
    LoginSerializer,
    PrintCenterProfileSerializer,
    PrintCenterStatusSerializer,
    RegisterPrintCenterSerializer,
    ShippingAutoRuleSerializer,
    SubUserCreateSerializer,
    SubUserProfileSerializer,
    UserSerializer,
)


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request):
        serializer = RegisterPrintCenterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {'detail': 'Registration successful. Awaiting admin approval.'},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = (AllowAny,)

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)


class LogoutView(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        data = UserSerializer(request.user).data
        if request.user.is_print_center:
            try:
                data['profile'] = PrintCenterProfileSerializer(request.user.print_center_profile).data
            except PrintCenterProfile.DoesNotExist:
                pass
        elif request.user.is_sub_user:
            try:
                data['profile'] = SubUserProfileSerializer(request.user.sub_user_profile).data
            except SubUserProfile.DoesNotExist:
                pass
        return Response(data)

    def patch(self, request):
        user = request.user
        if 'full_name' in request.data:
            user.full_name = request.data['full_name']
            user.save(update_fields=['full_name'])
        if 'password' in request.data:
            user.set_password(request.data['password'])
            user.save(update_fields=['password'])
        return Response(UserSerializer(user).data)


# ── SuperAdmin — Print Center Management ──────────────────────────────────────

class PrintCenterListView(generics.ListAPIView):
    permission_classes = (IsSuperAdmin,)
    serializer_class = PrintCenterProfileSerializer
    queryset = PrintCenterProfile.objects.select_related('user').order_by('-created_at')
    search_fields = ('company_name', 'user__email')
    filterset_fields = ('status',)


class PrintCenterDetailView(APIView):
    permission_classes = (IsSuperAdmin,)

    def _get_object(self, pk):
        try:
            return PrintCenterProfile.objects.select_related('user').get(pk=pk)
        except PrintCenterProfile.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        return Response(PrintCenterProfileSerializer(self._get_object(pk)).data)

    def patch(self, request, pk):
        pc = self._get_object(pk)
        serializer = PrintCenterStatusSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if 'status' in data:
            pc.status = data['status']
            pc.user.is_active = data['status'] == PrintCenterProfile.Status.ACTIVE
            pc.user.save(update_fields=['is_active'])
        if 'max_sub_users' in data:
            pc.max_sub_users = data['max_sub_users']
        pc.save()
        return Response(PrintCenterProfileSerializer(pc).data)


# ── Print Center — Sub User Management ───────────────────────────────────────

class SubUserListCreateView(APIView):
    permission_classes = (IsPrintCenter,)

    def get(self, request):
        qs = SubUserProfile.objects.filter(
            print_center=request.user.print_center_profile
        ).select_related('user').order_by('-created_at')
        return Response(SubUserProfileSerializer(qs, many=True).data)

    def post(self, request):
        serializer = SubUserCreateSerializer(
            data=request.data,
            context={'request': request, 'print_center': request.user.print_center_profile},
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            SubUserProfileSerializer(user.sub_user_profile).data,
            status=status.HTTP_201_CREATED,
        )


class SubUserDetailView(APIView):
    permission_classes = (IsPrintCenter,)

    def _get_object(self, request, pk):
        try:
            return SubUserProfile.objects.select_related('user').get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()

    def get(self, request, pk):
        return Response(SubUserProfileSerializer(self._get_object(request, pk)).data)

    def patch(self, request, pk):
        profile = self._get_object(request, pk)
        serializer = SubUserProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SubUserToggleActiveView(APIView):
    permission_classes = (IsPrintCenter,)

    def post(self, request, pk):
        try:
            profile = SubUserProfile.objects.select_related('user').get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()
        profile.is_active = not profile.is_active
        profile.user.is_active = profile.is_active
        profile.save(update_fields=['is_active'])
        profile.user.save(update_fields=['is_active'])
        return Response({'is_active': profile.is_active})


class AddBalanceView(APIView):
    permission_classes = (IsPrintCenter,)

    @transaction.atomic
    def post(self, request, pk):
        try:
            profile = SubUserProfile.objects.select_for_update().get(
                pk=pk, print_center=request.user.print_center_profile
            )
        except SubUserProfile.DoesNotExist:
            raise NotFound()
        serializer = AddBalanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data['amount']
        profile.balance += amount
        profile.save(update_fields=['balance'])
        tx = BalanceTransaction.objects.create(
            sub_user=profile,
            type=BalanceTransaction.Type.CREDIT,
            amount=amount,
            description=serializer.validated_data.get('description', ''),
            created_by=request.user,
        )
        return Response(BalanceTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class SubUserTransactionsView(generics.ListAPIView):
    permission_classes = (IsPrintCenter,)
    serializer_class = BalanceTransactionSerializer

    def get_queryset(self):
        if not SubUserProfile.objects.filter(
            pk=self.kwargs['pk'],
            print_center=self.request.user.print_center_profile
        ).exists():
            raise NotFound()
        return BalanceTransaction.objects.filter(
            sub_user__pk=self.kwargs['pk']
        ).order_by('-created_at')


# ── Sub User — Self ───────────────────────────────────────────────────────────

class MyTransactionsView(generics.ListAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = BalanceTransactionSerializer

    def get_queryset(self):
        return BalanceTransaction.objects.filter(
            sub_user=self.request.user.sub_user_profile
        ).order_by('-created_at')


class MyShippingRulesView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        rules = request.user.sub_user_profile.shipping_rules.all()
        return Response(ShippingAutoRuleSerializer(rules, many=True).data)

    def post(self, request):
        serializer = ShippingAutoRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(sub_user=request.user.sub_user_profile)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MyShippingRuleDeleteView(APIView):
    permission_classes = (IsAuthenticated,)

    def delete(self, request, rule_id):
        try:
            rule = request.user.sub_user_profile.shipping_rules.get(pk=rule_id)
        except Exception:
            raise NotFound()
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
