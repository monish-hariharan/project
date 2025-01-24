import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { supabase } from '../lib/supabase';
import { User, LogOut, Calendar } from 'lucide-react';
import { format, parseISO, differenceInHours } from 'date-fns';
import CancellationModal from '../components/CancellationModal';
import PaymentSimulation from '../components/PaymentSimulation';

type Profile = {
  id: string;
  full_name: string | null;
  phone_number: string | null;
  address: string | null;
  driver_license: string | null;
};

type Booking = {
  id: string;
  start_date: string;
  end_date: string;
  total_amount: number;
  status: string;
  payment_status: string;
  cancelled_at?: string;
  cancellation_fee?: number;
  refund_amount?: number;
  car: {
    make: string;
    model: string;
    year: number;
  };
};

const Account = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<any>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [isEditing, setIsEditing] = useState(false);
  const [formData, setFormData] = useState<Partial<Profile>>({});
  const [showCancellationModal, setShowCancellationModal] = useState(false);
  const [selectedBooking, setSelectedBooking] = useState<Booking | null>(null);
  const [showPayment, setShowPayment] = useState(false);

  useEffect(() => {
    checkUser();
  }, []);

  const checkUser = async () => {
    try {
      const { data: { user } } = await supabase.auth.getUser();
      if (!user) {
        navigate('/auth');
        return;
      }
      
      setUser(user);
      await Promise.all([fetchProfile(user.id), fetchBookings(user.id)]);
    } catch (error) {
      console.error('Error checking user:', error);
      navigate('/auth');
    } finally {
      setLoading(false);
    }
  };

  const fetchProfile = async (userId: string) => {
    try {
      let { data: profile, error } = await supabase
        .from('profiles')
        .select('*')
        .eq('id', userId)
        .single();

      if (error) {
        if (error.code === 'PGRST116') {
          // Profile doesn't exist, create it
          const { data: newProfile, error: insertError } = await supabase
            .from('profiles')
            .insert([{
              id: userId,
              full_name: null,
              phone_number: null,
              address: null,
              driver_license: null
            }])
            .select()
            .single();

          if (insertError) throw insertError;
          profile = newProfile;
        } else {
          throw error;
        }
      }

      setProfile(profile);
      setFormData(profile);
    } catch (error) {
      console.error('Error fetching profile:', error);
    }
  };

  const fetchBookings = async (userId: string) => {
    try {
      const { data, error } = await supabase
        .from('bookings')
        .select(`
          *,
          car:cars(make, model, year)
        `)
        .eq('user_id', userId)
        .order('created_at', { ascending: false });

      if (error) throw error;
      setBookings(data || []);
    } catch (error) {
      console.error('Error fetching bookings:', error);
    }
  };

  const handleUpdateProfile = async () => {
    try {
      const { error } = await supabase
        .from('profiles')
        .update(formData)
        .eq('id', user.id);

      if (error) throw error;
      setProfile({ ...profile, ...formData } as Profile);
      setIsEditing(false);
    } catch (error) {
      console.error('Error updating profile:', error);
    }
  };

  const handleSignOut = async () => {
    try {
      const { error } = await supabase.auth.signOut();
      if (error) throw error;
      navigate('/auth');
    } catch (error) {
      console.error('Error signing out:', error);
    }
  };

  const handleCancelBooking = async (booking: Booking) => {
    setSelectedBooking(booking);
    setShowCancellationModal(true);
  };

  const processCancellation = async () => {
    if (!selectedBooking) return;

    try {
      const hoursUntilStart = differenceInHours(
        new Date(selectedBooking.start_date),
        new Date()
      );

      const cancellationFee = hoursUntilStart < 24 ? 100 : 0;
      const refundAmount = selectedBooking.total_amount - cancellationFee;

      const { error } = await supabase
        .from('bookings')
        .update({
          status: 'cancelled',
          cancelled_at: new Date().toISOString(),
          cancellation_fee: cancellationFee,
          refund_amount: refundAmount
        })
        .eq('id', selectedBooking.id);

      if (error) throw error;

      await fetchBookings(user.id);
      setShowCancellationModal(false);
      setSelectedBooking(null);
    } catch (error) {
      console.error('Error cancelling booking:', error);
    }
  };

  const handlePayment = async (booking: Booking) => {
    setSelectedBooking(booking);
    setShowPayment(true);
  };

  const handlePaymentSuccess = async () => {
    if (!selectedBooking) return;

    try {
      const { error } = await supabase
        .from('bookings')
        .update({
          payment_status: 'paid'
        })
        .eq('id', selectedBooking.id);

      if (error) throw error;

      await fetchBookings(user.id);
      setShowPayment(false);
      setSelectedBooking(null);
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

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="max-w-7xl mx-auto px-4 py-8"
    >
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Profile Section */}
        <div className="lg:col-span-1">
          <div className="bg-white rounded-lg shadow-md p-6">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-2xl font-bold">Profile</h2>
              <button
                onClick={handleSignOut}
                className="text-red-600 hover:text-red-700 flex items-center space-x-1"
              >
                <LogOut className="h-5 w-5" />
                <span>Sign Out</span>
              </button>
            </div>
            {isEditing ? (
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Full Name
                  </label>
                  <input
                    type="text"
                    value={formData.full_name || ''}
                    onChange={(e) => setFormData({ ...formData, full_name: e.target.value })}
                    className="w-full border rounded-lg p-2 focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Phone Number
                  </label>
                  <input
                    type="tel"
                    value={formData.phone_number || ''}
                    onChange={(e) => setFormData({ ...formData, phone_number: e.target.value })}
                    className="w-full border rounded-lg p-2 focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Address
                  </label>
                  <textarea
                    value={formData.address || ''}
                    onChange={(e) => setFormData({ ...formData, address: e.target.value })}
                    className="w-full border rounded-lg p-2 focus:ring-2 focus:ring-blue-500"
                    rows={3}
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Driver's License
                  </label>
                  <input
                    type="text"
                    value={formData.driver_license || ''}
                    onChange={(e) => setFormData({ ...formData, driver_license: e.target.value })}
                    className="w-full border rounded-lg p-2 focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div className="flex space-x-4">
                  <button
                    onClick={handleUpdateProfile}
                    className="flex-1 bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => {
                      setIsEditing(false);
                      setFormData(profile || {});
                    }}
                    className="flex-1 border border-gray-300 text-gray-700 py-2 rounded-lg hover:bg-gray-50"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Full Name</p>
                    <p className="font-medium">{profile?.full_name || 'Not set'}</p>
                  </div>
                  <User className="h-5 w-5 text-gray-400" />
                </div>
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-sm text-gray-600">Phone Number</p>
                    <p className="font-medium">{profile?.phone_number || 'Not set'}</p>
                  </div>
                  <Calendar className="h-5 w-5 text-gray-400" />
                </div>
                <div>
                  <p className="text-sm text-gray-600">Address</p>
                  <p className="font-medium">{profile?.address || 'Not set'}</p>
                </div>
                <div>
                  <p className="text-sm text-gray-600">Driver's License</p>
                  <p className="font-medium">{profile?.driver_license || 'Not set'}</p>
                </div>
                <button
                  onClick={() => setIsEditing(true)}
                  className="w-full bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700"
                >
                  Edit Profile
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Bookings Section */}
        <div className="lg:col-span-2">
          <div className="bg-white rounded-lg shadow-md p-6">
            <h2 className="text-2xl font-bold mb-6">My Bookings</h2>
            <div className="space-y-4">
              {bookings.length === 0 ? (
                <p className="text-gray-600 text-center py-8">No bookings found</p>
              ) : (
                bookings.map((booking) => (
                  <div
                    key={booking.id}
                    className="border rounded-lg p-4 hover:border-blue-500 transition-colors"
                  >
                    <div className="flex justify-between items-start mb-4">
                      <div>
                        <h3 className="font-semibold">
                          {booking.car.year} {booking.car.make} {booking.car.model}
                        </h3>
                        <p className="text-sm text-gray-600">
                          {format(parseISO(booking.start_date), 'PPP')} - {format(parseISO(booking.end_date), 'PPP')}
                        </p>
                      </div>
                      <div className="text-right">
                        <p className="font-semibold text-blue-600">
                          £{booking.total_amount}
                        </p>
                        <p className="text-sm text-gray-600">
                          Status: {booking.status}
                        </p>
                      </div>
                    </div>

                    {booking.status === 'cancelled' ? (
                      <div className="text-sm text-gray-600">
                        <p>Cancelled on {format(parseISO(booking.cancelled_at!), 'PPP')}</p>
                        {booking.cancellation_fee > 0 && (
                          <p>Cancellation fee: £{booking.cancellation_fee}</p>
                        )}
                        {booking.refund_amount > 0 && (
                          <p>Refund amount: £{booking.refund_amount}</p>
                        )}
                      </div>
                    ) : (
                      <div className="flex justify-between items-center">
                        {booking.payment_status === 'unpaid' ? (
                          <button
                            onClick={() => handlePayment(booking)}
                            className="text-blue-600 hover:text-blue-700 font-medium"
                          >
                            Pay Now
                          </button>
                        ) : (
                          <button
                            onClick={() => handleCancelBooking(booking)}
                            className="text-red-600 hover:text-red-700 font-medium"
                          >
                            Cancel Booking
                          </button>
                        )}
                        <span className="text-sm text-gray-600">
                          Payment: {booking.payment_status}
                        </span>
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Modals */}
      {showCancellationModal && selectedBooking && (
        <CancellationModal
          startDate={selectedBooking.start_date}
          onConfirm={processCancellation}
          onClose={() => {
            setShowCancellationModal(false);
            setSelectedBooking(null);
          }}
        />
      )}

      {showPayment && selectedBooking && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-lg w-full max-w-md">
            <PaymentSimulation
              amount={selectedBooking.total_amount}
              onSuccess={handlePaymentSuccess}
              onCancel={() => {
                setShowPayment(false);
                setSelectedBooking(null);
              }}
            />
          </div>
        </div>
      )}
    </motion.div>
  );
};

export default Account;