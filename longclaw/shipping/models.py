import datetime
import decimal
import hashlib
import json
import uuid

from django.db import models
from django.utils import timezone
from django.utils.encoding import force_bytes

from wagtail.admin.edit_handlers import FieldPanel
from wagtail.snippets.models import register_snippet

from longclaw.configuration.models import Configuration
from longclaw.shipping.utils import get_shipping_cost, InvalidShippingRate, InvalidShippingCountry


@register_snippet
class Address(models.Model):
    name = models.CharField(max_length=64)
    line_1 = models.CharField(max_length=128)
    line_2 = models.CharField(max_length=128, blank=True)
    city = models.CharField(max_length=64)
    postcode = models.CharField(max_length=10)
    country = models.ForeignKey('shipping.Country', blank=True, null=True, on_delete=models.PROTECT)

    panels = [
        FieldPanel('name'),
        FieldPanel('line_1'),
        FieldPanel('line_2'),
        FieldPanel('city'),
        FieldPanel('postcode'),
        FieldPanel('country')
    ]

    def __str__(self):
        return "{}, {}, {}".format(self.name, self.city, self.country)

class ShippingRate(models.Model):
    """
    An individual shipping rate. This can be applied to
    multiple countries.
    """
    name = models.CharField(
        max_length=32,
        unique=True,
        help_text="Unique name to refer to this shipping rate by"
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    carrier = models.CharField(max_length=64)
    description = models.CharField(max_length=128)
    countries = models.ManyToManyField('shipping.Country')

    panels = [
        FieldPanel('name'),
        FieldPanel('rate'),
        FieldPanel('carrier'),
        FieldPanel('description'),
        FieldPanel('countries')
    ]

    def __str__(self):
        return self.name

class Country(models.Model):
    """
    International Organization for Standardization (ISO) 3166-1 Country list
    Instance Variables:
    iso -- ISO 3166-1 alpha-2
    name -- Official country names (in all caps) used by the ISO 3166
    display_name -- Country names in title format
    sort_priority -- field that allows for customizing the default ordering
    0 is the default value, and the higher the value the closer to the
    beginning of the list it will be.  An example use case would be you will
    primarily have addresses for one country, so you want that particular
    country to be the first option in an html dropdown box.  To do this, you
    would simply change the value in the json file or alter
    country_grabber.py's priority dictionary and run it to regenerate
    the json
    """
    iso = models.CharField(max_length=2, primary_key=True)
    name_official = models.CharField(max_length=128)
    name = models.CharField(max_length=128)
    sort_priority = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = 'Countries'
        ordering = ('-sort_priority', 'name',)

    def __str__(self):
        """ Return the display form of the country name"""
        return self.name

class AbstractShippingQuote(models.Model):
    """
    Abstract shipping quote for a particular basket
    """
    class Meta:
        unique_together = (
            ('basket_id', 'is_selected', 'key'),
        )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    basket_id = models.CharField(max_length=32)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(
        blank=True,
        default=lambda: timezone.now() + datetime.timedelta(days=30),
        null=True,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    carrier = models.CharField(max_length=255)
    service = models.CharField(max_length=255)
    description = models.TextField()
    key = models.CharField(max_length=255, editable=False)
    is_selected = models.NullBooleanField()
    is_valid = models.BooleanField()
    
    @classmethod
    def generate_key(cls, destination, basket_id, site):
        raise NotImplementedError()
    
    @classmethod
    def create_shipping_quotes(cls, destination, basket_id, site):
        raise NotImplementedError()
    
    def get_shipping_quotes(self, destination, basket_id, site):
        raise NotImplementedError()
    
    def get_selected_shipping_quote(self, destination, basket_id, site):
        raise NotImplementedError()
    
    def select_shipping_quote(self, destination, basket_id, quote_id, site):
        raise NotImplementedError()
    
    def remove_expired_shipping_quotes(self):
        raise NotImplementedError()
    
    def can_book(self, destination, basket_id, site):
        return self.is_valid and timezone.now() < self.expires_at and self.key == self.generate_key(destination, basket_id, site)

class DefaultShippingQuote(AbstractShippingQuote):
    """
    Default shipping implementation which integrates the ShippingRate model
    """
    @classmethod
    def generate_key(cls, destination, basket_id, site):
        data = {
            'destination_country': destination.country.pk,
            'site': site.pk if site else site,
        }
        datastring = json.dumps(data, sort_keys=True, indent=4, separators=(',', ': '))
        databytes = force_bytes(datastring)
        digest = hashlib.sha1(databytes).hexdigest()
        return digest
    
    @classmethod
    def create_shipping_quotes(cls, destination, basket_id, site):
        site_settings = Configuration.for_site(site)
        
        # This extra query is a hack to tap into existing code and should be optimized
        shipping_rate_names = ShippingRate.objects.filter(
            countries__in=[destination.country.pk]
        ).values_list('name', flat=True)
        
        quotes = []
        key = cls.generate_key(destination, basket_id, site)
        
        for name in shipping_rate_names:
            lookups = dict(
                basket_id = basket_id,
                service = name,
                key = key,
            )
            valid = True
            try:
                shipping_rate = get_shipping_cost(
                    site_settings,
                    destination.country.pk,
                    name,
                )
            except InvalidShippingRate, InvalidShippingCountry:
                shipping_rate = {
                    'amount': decimal.Decimal(0),
                    'carrier': 'INVALID',
                    'description': 'INVALID',
                }
                valid = False
                
            details = dict(
                amount = shipping_rate["rate"],
                carrier = shipping_rate["carrier"],
                description = shipping_rate["description"],
                is_valid = valid,
            )
            instance = cls.objects.update_or_create(defaults=lookups, **details)
            quotes.append(instance)
        
        if len(quotes) == 1:
            instance = quotes[0]
            instance.is_selected = True
            instance.save()
        
        return quotes
