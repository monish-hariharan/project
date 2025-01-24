/*
  # Fix booking extras RLS policies

  1. Changes
    - Add INSERT policy for booking_extras table
    - Ensures users can only add extras to their own bookings
    
  2. Security
    - Maintains RLS protection
    - Only allows users to add extras to bookings they own
*/

CREATE POLICY "Users can insert their booking extras"
  ON booking_extras FOR INSERT
  TO authenticated
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM bookings
      WHERE bookings.id = booking_extras.booking_id
      AND bookings.user_id = auth.uid()
    )
  );