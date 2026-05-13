from rest_framework import serializers


class RateRequestSerializer(serializers.Serializer):
    ship_to_country = serializers.CharField(max_length=2)
    ship_to_postal_code = serializers.CharField(max_length=20)
    weight_oz = serializers.FloatField()
    length_in = serializers.FloatField(required=False, default=10)
    width_in = serializers.FloatField(required=False, default=8)
    height_in = serializers.FloatField(required=False, default=1)
