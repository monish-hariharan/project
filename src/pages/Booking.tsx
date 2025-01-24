import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Calendar, Clock, Plus, Minus, CreditCard } from 'lucide-react';
import { supabase } from '../lib/supabase';
import { format, addDays, differenceInDays } from 'date-fns';
import PaymentSimulation from '../components/PaymentSimulation';

type Car = {
  id: string;
  make: string;
  model: string;
  year: number;
  daily_rate: number;
  image_url: string;
  category: string;
  seats: number;
  transmission: string;
  fuel_type: string;
};

type Extra = {
  id: string;
  name: string;
  description: string;
  daily_rate: number;
};

const Booking = () => {
  const { carId } = useParams();
  const navigate = useNavigate();
  const [car, setCar] = useState<Car | null>(null);
  const [extras, setExtras] = useState<Extra[]>([]);
  const [selectedExtras, setSelectedExtras] = useState<string[]>([]);
  const [startDate, setStartDate] = useState(format(new Date(), 'yyyy-MM-dd'));
  const [endDate, setEndDate] = useState(format(addDays(new Date(), 3), 'yyyy-MM-dd'));
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<any>(null);
  const [showPayment, setShowPayment] = useState(false);
  const [bookingId, setBookingId] = useState<string | null>(null);

  useEffect(() => {
    fetchCarAndExtras();
    checkUser();
  }, [carId]);

  const checkUser = async () => {
    const { data: { user } } = await supabase.auth.getUser();
    setUser(user);
  };

  const fetchCarAndExtras = async () => {
    try {
      const [carResponse, extrasResponse] = await Promise.all([
        supabase.from('cars').select('*').eq('id', carId).single(),
        supabase.from('extras').select('*')
      ]);

      if (carResponse.error) throw carResponse.error;
      if (extrasResponse.error) throw extrasResponse.error;

      setCar(carResponse.data);
      setExtras(extrasResponse.data || []);
    } catch (error) {
      console.error('Error fetching data:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleExtraToggle = (extraId: string) => {
    setSelectedExtras(prev =>
      prev.includes(extraId)
        ? prev.filter(id => id !== extraId)
        : [...prev, extraId]
    );
  };

  const calculateTotal = () => {
    if (!car) return 0;
    const days = differenceInDays(new Date(endDate), new Date(startDate)) + 1;
    const carTotal = car.daily_rate * days;
    const extrasTotal = selectedExtras.reduce((total, extraId) => {
      const extra = extras.find(e => e.id === extraId);
      return total + (extra ? extra.daily_rate * days : 0);
    }, 0);
    return carTotal + extrasTotal;
  };

  const handleBooking = async () => {
    if (!user) {
      navigate('/auth');
      return;
    }

    try {
      const { data, error } = await supabase
        .from('bookings')
        .insert({
          user_id: user.id,
          car_id: carId,
          start_date: startDate,
          end_date: endDate,
          total_amount: calculateTotal(),
          status: 'pending',
          payment_status: 'unpaid'
        })
        .select()
        .single();

      if (error) throw error;

      if (selectedExtras.length > 0) {
        const bookingExtras = selectedExtras.map(extraId => ({
          booking_id: data.id,
          extra_id: extraId
        }));

        const { error: extrasError } = await supabase
          .from('booking_extras')
          .insert(bookingExtras);

        if (extrasError) throw extrasError;
      }

      setBookingId(data.id);
      setShowPayment(true);
    } catch (error) {
      console.error('Error creating booking:', error);
    }
  };

  const handlePaymentSuccess = async () => {
    if (!bookingId) return;

    try {
      const { error } = await supabase
        .from('bookings')
        .update({
          payment_status: 'paid',
          status: 'confirmed'
        })
        .eq('id', bookingId);

      if (error) throw error;

      // Redirect to account page to view the booking
      navigate('/account');
    } catch (error) {
      console.error('Error updating payment status:', error);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center items-center min-h-screen">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  if (!car) {
    return (
      <div className="max-w-7xl mx-auto px-4 py-12 text-center">
        <h2 className="text-2xl font-bold text-gray-900">Car not found</h2>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="max-w-7xl mx-auto px-4 py-8"
    >
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {/* Car Details */}
        <div className="bg-white rounded-lg shadow-md overflow-hidden">
          <img
            src={car.image_url}
            alt={`${car.make} ${car.model}`}
            className="w-full h-64 object-cover"
          />
          <div className="p-6">
            <h2 className="text-2xl font-bold mb-4">
              {car.year} {car.make} {car.model}
            </h2>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div>
                <span className="font-medium">Category:</span> {car.category}
              </div>
              <div>
                <span className="font-medium">Seats:</span> {car.seats}
              </div>
              <div>
                <span className="font-medium">Transmission:</span> {car.transmission}
              </div>
              <div>
                <span className="font-medium">Fuel Type:</span> {car.fuel_type}
              </div>
            </div>
          </div>
        </div>

        {/* Booking Form */}
        <div className="space-y-6">
          {/* Dates */}
          <div className="bg-white rounded-lg shadow-md p-6">
            <h3 className="text-lg font-semibold mb-4">Select Dates</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Start Date
                </label>
                <input
                  type="date"
                  value={startDate}
                  min={format(new Date(), 'yyyy-MM-dd')}
                  onChange={(e) => setStartDate(e.target.value)}
                  className="w-full border rounded-lg p-2 focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  End Date
                </label>
                <input
                  type="date"
                  value={endDate}
                  min={startDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  className="w-full border rounded-lg p-2 focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
          </div>

          {/* Extras */}
          <div className="bg-white rounded-lg shadow-md p-6">
            <h3 className="text-lg font-semibold mb-4">Add Extras</h3>
            <div className="space-y-4">
              {extras.map((extra) => (
                <div
                  key={extra.id}
                  className="flex items-center justify-between p-4 border rounded-lg hover:border-blue-500 cursor-pointer"
                  onClick={() => handleExtraToggle(extra.id)}
                >
                  <div>
                    <h4 className="font-medium">{extra.name}</h4>
                    <p className="text-sm text-gray-600">{extra.description}</p>
                  </div>
                  <div className="flex items-center space-x-4">
                    <span className="text-blue-600">£{extra.daily_rate}/day</span>
                    {selectedExtras.includes(extra.id) ? (
                      <Minus className="h-6 w-6 text-blue-600" />
                    ) : (
                      <Plus className="h-6 w-6 text-gray-400" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Total and Booking Button */}
          <div className="bg-white rounded-lg shadow-md p-6">
            <div className="flex justify-between items-center mb-4">
              <span className="text-lg font-semibold">Total Amount</span>
              <span className="text-2xl font-bold text-blue-600">
                £{calculateTotal().toFixed(2)}
              </span>
            </div>
            <button
              onClick={handleBooking}
              className="w-full bg-blue-600 text-white py-3 rounded-lg hover:bg-blue-700 transition-colors flex items-center justify-center space-x-2"
            >
              <CreditCard className="h-5 w-5" />
              <span>{user ? 'Proceed to Payment' : 'Sign in to Book'}</span>
            </button>
          </div>
        </div>
      </div>

      {/* Payment Modal */}
      {showPayment && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-lg w-full max-w-md">
            <PaymentSimulation
              amount={calculateTotal()}
              onSuccess={handlePaymentSuccess}
              onCancel={() => {
                setShowPayment(false);
                setBookingId(null);
              }}
            />
          </div>
        </div>
      )}
    </motion.div>
  );
};

export default Booking;