/*
  # Car Rental Database Schema

  1. New Tables
    - `profiles`
      - Extends Supabase auth.users
      - Stores additional user information
    - `cars`
      - Stores available cars for rent
      - Includes details like make, model, year, price
    - `extras`
      - Additional services/products
      - GPS, child seats, insurance packages
    - `bookings`
      - Stores rental bookings
      - Links users, cars, and extras
    - `booking_extras`
      - Junction table for bookings and extras
      
  2. Security
    - RLS enabled on all tables
    - Policies for user data protection
    - Public read access for cars and extras
*/

-- Create tables
CREATE TABLE profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id),
  full_name text,
  phone_number text,
  address text,
  driver_license text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE cars (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  make text NOT NULL,
  model text NOT NULL,
  year integer NOT NULL,
  color text NOT NULL,
  daily_rate decimal(10,2) NOT NULL,
  image_url text NOT NULL,
  category text NOT NULL,
  seats integer NOT NULL,
  transmission text NOT NULL,
  fuel_type text NOT NULL,
  available boolean DEFAULT true,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE extras (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  description text NOT NULL,
  daily_rate decimal(10,2) NOT NULL,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE bookings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES profiles(id) NOT NULL,
  car_id uuid REFERENCES cars(id) NOT NULL,
  start_date date NOT NULL,
  end_date date NOT NULL,
  total_amount decimal(10,2) NOT NULL,
  status text DEFAULT 'pending',
  payment_status text DEFAULT 'unpaid',
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE booking_extras (
  booking_id uuid REFERENCES bookings(id) ON DELETE CASCADE,
  extra_id uuid REFERENCES extras(id) ON DELETE CASCADE,
  PRIMARY KEY (booking_id, extra_id)
);

-- Enable RLS
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE cars ENABLE ROW LEVEL SECURITY;
ALTER TABLE extras ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE booking_extras ENABLE ROW LEVEL SECURITY;

-- Policies
CREATE POLICY "Users can view their own profile"
  ON profiles FOR SELECT
  TO authenticated
  USING (auth.uid() = id);

CREATE POLICY "Users can update their own profile"
  ON profiles FOR UPDATE
  TO authenticated
  USING (auth.uid() = id);

CREATE POLICY "Public can view cars"
  ON cars FOR SELECT
  TO anon, authenticated
  USING (true);

CREATE POLICY "Public can view extras"
  ON extras FOR SELECT
  TO anon, authenticated
  USING (true);

CREATE POLICY "Users can view their own bookings"
  ON bookings FOR SELECT
  TO authenticated
  USING (user_id = auth.uid());

CREATE POLICY "Users can create their own bookings"
  ON bookings FOR INSERT
  TO authenticated
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "Users can view their booking extras"
  ON booking_extras FOR SELECT
  TO authenticated
  USING (EXISTS (
    SELECT 1 FROM bookings
    WHERE bookings.id = booking_extras.booking_id
    AND bookings.user_id = auth.uid()
  ));